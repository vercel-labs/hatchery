"""Durable worker and task records in Postgres or local JSON files."""

import json
import threading
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


async def apply_event(event) -> tuple[models.Task | None, bool]:
    """Apply one ordered event. Duplicate IDs and stale sequences are ignored."""
    if event.task_id is None:
        return None, False
    task = await get_task(event.task_id)
    if task is None:
        return None, False
    with _lock:
        # The local lock covers file mode. Postgres still gets deterministic,
        # idempotent writes; a later transaction can tighten concurrent delivery.
        if event.id in task.event_ids or event.sequence <= task.event_sequence:
            return task, False
        task.event_ids = [*task.event_ids[-99:], event.id]
        task.event_sequence = event.sequence
        states = {
            "task.started": "running",
            "task.question": "attention",
            "task.completed": "complete",
            "task.failed": "errored",
        }
        if event.type in states:
            task.status = states[event.type]
        if event.type == "task.question":
            task.result = {"question": event.payload.get("question") or event.payload.get("text")}
        elif event.type == "task.completed":
            task.result = event.payload.get("result") or {"summary": event.payload.get("summary")}
        elif event.type == "task.failed":
            task.result = {"error": event.payload.get("error") or "worker task failed"}
        task.updated_at = event.created_at
    return await save_task(task), True


async def _save(kind: str, item_id: str, data: str, *, chat_id: str, worker_id: str | None = None) -> None:
    if store.use_postgres():
        from store import db

        table = "hatchery_workers" if kind == "workers" else "hatchery_worker_tasks"
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

        table = "hatchery_workers" if kind == "workers" else "hatchery_worker_tasks"
        row = await (await db.pool()).fetchrow(f"SELECT data FROM {table} WHERE id = $1", item_id)
        if row is None:
            return None
        return row["data"] if isinstance(row["data"], str) else json.dumps(row["data"])
    path = _path(kind, item_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


async def _list(kind: str, chat_id: str | None) -> list[str]:
    if store.use_postgres():
        from store import db

        table = "hatchery_workers" if kind == "workers" else "hatchery_worker_tasks"
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

        table = "hatchery_workers" if kind == "workers" else "hatchery_worker_tasks"
        result = await (await db.pool()).execute(f"DELETE FROM {table} WHERE id = $1", item_id)
        return result != "DELETE 0"
    path = _path(kind, item_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def _path(kind: str, item_id: str):
    return store.data_dir() / kind / f"{urllib.parse.quote(item_id, safe='')}.json"
