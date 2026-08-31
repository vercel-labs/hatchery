"""Durable worker and task records in Postgres or local JSON files."""

import json
import threading
import typing
import urllib.parse

import pydantic

import store
from worker import models

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_workers (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_workers_chat ON hatchery_workers (chat_id, created_at);
CREATE TABLE IF NOT EXISTS hatchery_worker_tasks (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    worker_id  TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_worker_tasks_chat ON hatchery_worker_tasks (chat_id, created_at);
CREATE TABLE IF NOT EXISTS hatchery_worker_terminals (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    worker_id  TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_worker_terminals_chat ON hatchery_worker_terminals (chat_id, created_at);
"""

_lock = threading.Lock()
_schema_ready = False


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "workers").mkdir(parents=True, exist_ok=True)
        (store.data_dir() / "worker_tasks").mkdir(parents=True, exist_ok=True)
        (store.data_dir() / "worker_terminals").mkdir(parents=True, exist_ok=True)


async def save(worker: models.Worker) -> models.Worker:
    await _save("workers", worker.id, worker.model_dump_json(), chat_id=worker.chat_id)
    return worker


async def get(worker_id: str) -> models.Worker | None:
    raw = await _get("workers", worker_id)
    if raw is None:
        return None
    try:
        return models.Worker.model_validate_json(raw)
    except pydantic.ValidationError:
        return None


async def list_all(chat_id: str | None = None) -> list[models.Worker]:
    return [models.Worker.model_validate_json(raw) for raw in await _list("workers", chat_id)]


async def delete(worker_id: str) -> bool:
    return await _delete("workers", worker_id)


async def save_task(task: models.Task) -> models.Task:
    await _save(
        "worker_tasks",
        task.id,
        task.model_dump_json(),
        chat_id=task.chat_id,
        worker_id=task.worker_id,
    )
    return task


async def create_task(task: models.Task) -> bool:
    """Insert one task without replacing an existing task of the same identity."""
    if store.use_postgres():
        from store import db

        result = await (await db.pool()).execute(
            "INSERT INTO hatchery_worker_tasks (id, chat_id, worker_id, data) "
            "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (id) DO NOTHING",
            task.id,
            task.chat_id,
            task.worker_id,
            task.model_dump_json(),
        )
        return result == "INSERT 0 1"
    with _lock:
        path = _path("worker_tasks", task.id)
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task.model_dump_json(), encoding="utf-8")
        return True


async def mutate_task(
    task_id: str,
    mutate: typing.Callable[[models.Task], models.Task | None],
) -> models.Task | None:
    """Atomically read, change, and save one task in either storage backend."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM hatchery_worker_tasks WHERE id = $1 FOR UPDATE",
                task_id,
            )
            if row is None:
                return None
            raw = row["data"] if isinstance(row["data"], str) else json.dumps(row["data"])
            task = models.Task.model_validate_json(raw)
            changed = mutate(task)
            if changed is None:
                return task
            await conn.execute(
                "UPDATE hatchery_worker_tasks SET chat_id = $2, worker_id = $3, data = $4::jsonb WHERE id = $1",
                changed.id,
                changed.chat_id,
                changed.worker_id,
                changed.model_dump_json(),
            )
            return changed
    with _lock:
        raw = await _get("worker_tasks", task_id)
        if raw is None:
            return None
        task = models.Task.model_validate_json(raw)
        changed = mutate(task)
        if changed is None:
            return task
        await _save(
            "worker_tasks",
            changed.id,
            changed.model_dump_json(),
            chat_id=changed.chat_id,
            worker_id=changed.worker_id,
        )
        return changed


async def get_task(task_id: str) -> models.Task | None:
    raw = await _get("worker_tasks", task_id)
    if raw is None:
        return None
    try:
        return models.Task.model_validate_json(raw)
    except pydantic.ValidationError:
        return None


async def list_tasks(chat_id: str | None = None) -> list[models.Task]:
    return [models.Task.model_validate_json(raw) for raw in await _list("worker_tasks", chat_id)]


async def delete_task(task_id: str) -> bool:
    return await _delete("worker_tasks", task_id)


async def save_terminal(terminal: models.Terminal) -> models.Terminal:
    await _save(
        "worker_terminals",
        terminal.id,
        terminal.model_dump_json(),
        chat_id=terminal.chat_id,
        worker_id=terminal.worker_id,
    )
    return terminal


async def get_terminal(terminal_id: str) -> models.Terminal | None:
    raw = await _get("worker_terminals", terminal_id)
    return models.Terminal.model_validate_json(raw) if raw is not None else None


async def list_terminals(chat_id: str | None = None) -> list[models.Terminal]:
    return [
        models.Terminal.model_validate_json(raw)
        for raw in await _list("worker_terminals", chat_id)
    ]


async def delete_terminal(terminal_id: str) -> bool:
    return await _delete("worker_terminals", terminal_id)


async def apply_event(event) -> tuple[models.Task | None, bool]:
    """Apply one event atomically. Late transcript events remain observable."""
    if event.task_id is None:
        return None, False
    changed = False

    def apply(task: models.Task) -> models.Task | None:
        nonlocal changed
        if event.id in task.event_ids:
            return None
        stale = event.sequence <= task.event_sequence
        task.event_ids.append(event.id)
        if stale and event.type not in ("task.output", "task.transcript"):
            return task
        changed = True
        task.event_sequence = max(task.event_sequence, event.sequence)
        task.last_agent_event_at = max(task.last_agent_event_at or "", event.created_at)
        if event.type == "task.started":
            task.status = "running"
            task.launch_attempts = 0
        elif event.type == "task.output":
            text = str(event.payload.get("text") or "").strip()
            if text:
                if not stale:
                    task.last_agent_words = text
                task.transcript_event_count += 1
                if len(text) > 8 * 1024:
                    task.transcript_truncated_count += 1
            session_id = event.payload.get("session_id")
            if session_id:
                task.fx_session_id = str(session_id)
            pull_request = event.payload.get("pull_request")
            if isinstance(pull_request, dict):
                url = str(pull_request.get("url") or "")
                if url and all(item.get("url") != url for item in task.pull_requests):
                    task.pull_requests.append({
                        "url": url,
                        "repo_path": str(pull_request.get("repo_path") or ""),
                    })
        elif event.type == "task.transcript":
            task.transcript_event_count += 1
            if event.payload.get("kind") == "tool.call":
                task.transcript_tool_call_count += 1
            if event.payload.get("truncated"):
                task.transcript_truncated_count += 1
            session_id = event.payload.get("session_id")
            if session_id:
                task.fx_session_id = str(session_id)
        elif event.type == "task.question":
            question = str(event.payload.get("question") or event.payload.get("text") or "input required")
            task.status = "attention"
            task.active_question = question
            task.active_question_id = event.id
            task.result = {"question": question}
        elif event.type == "task.completed":
            task.status = "complete"
            task.active_question = None
            task.active_question_id = None
            task.result = event.payload.get("result") or {
                "summary": event.payload.get("summary") or task.last_agent_words or "subagent completed"
            }
            if task.pull_requests:
                task.result["pull_requests"] = task.pull_requests
        elif event.type == "task.failed":
            task.status = "errored"
            task.result = {"error": event.payload.get("error") or "worker task failed"}
        task.updated_at = max(task.updated_at, event.created_at)
        return task

    task = await mutate_task(event.task_id, apply)
    return task, changed


async def _save(kind: str, item_id: str, data: str, *, chat_id: str, worker_id: str | None = None) -> None:
    if store.use_postgres():
        from store import db

        table = {
            "workers": "hatchery_workers",
            "worker_tasks": "hatchery_worker_tasks",
            "worker_terminals": "hatchery_worker_terminals",
        }[kind]
        if kind == "workers":
            query = f"INSERT INTO {table} (id, chat_id, data) VALUES ($1, $2, $3::jsonb) ON CONFLICT (id) DO UPDATE SET chat_id = EXCLUDED.chat_id, data = EXCLUDED.data"
            args = (item_id, chat_id, data)
        else:
            query = f"INSERT INTO {table} (id, chat_id, worker_id, data) VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (id) DO UPDATE SET chat_id = EXCLUDED.chat_id, worker_id = EXCLUDED.worker_id, data = EXCLUDED.data"
            args = (item_id, chat_id, worker_id, data)
        await (await db.pool()).execute(query, *args)
        return
    path = _path(kind, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


async def _get(kind: str, item_id: str) -> str | None:
    if store.use_postgres():
        from store import db

        table = {"workers": "hatchery_workers", "worker_tasks": "hatchery_worker_tasks", "worker_terminals": "hatchery_worker_terminals"}[kind]
        row = await (await db.pool()).fetchrow(f"SELECT data FROM {table} WHERE id = $1", item_id)
        if row is None:
            return None
        return row["data"] if isinstance(row["data"], str) else json.dumps(row["data"])
    path = _path(kind, item_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


async def _list(kind: str, chat_id: str | None) -> list[str]:
    if store.use_postgres():
        from store import db

        table = {"workers": "hatchery_workers", "worker_tasks": "hatchery_worker_tasks", "worker_terminals": "hatchery_worker_terminals"}[kind]
        query = f"SELECT data FROM {table}"
        args = ()
        if chat_id is not None:
            query += " WHERE chat_id = $1"
            args = (chat_id,)
        rows = await (await db.pool()).fetch(query + " ORDER BY created_at", *args)
        return [row["data"] if isinstance(row["data"], str) else json.dumps(row["data"]) for row in rows]
    found = []
    for path in sorted((store.data_dir() / kind).glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        if chat_id is None or json.loads(raw).get("chat_id") == chat_id:
            found.append(raw)
    found.sort(key=lambda raw: json.loads(raw).get("created_at", ""))
    return found


async def _delete(kind: str, item_id: str) -> bool:
    if store.use_postgres():
        from store import db

        table = {"workers": "hatchery_workers", "worker_tasks": "hatchery_worker_tasks", "worker_terminals": "hatchery_worker_terminals"}[kind]
        result = await (await db.pool()).execute(f"DELETE FROM {table} WHERE id = $1", item_id)
        return result != "DELETE 0"
    path = _path(kind, item_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def _path(kind: str, item_id: str):
    return store.data_dir() / kind / f"{urllib.parse.quote(item_id, safe='')}.json"
