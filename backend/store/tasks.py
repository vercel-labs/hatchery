"""Durable coder launches, one record per task and many tasks per chat."""

import datetime
import json
import threading
import urllib.parse
import uuid

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS fab_tasks (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fab_tasks_chat ON fab_tasks (chat_id, created_at);
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
        (store.data_dir() / "tasks").mkdir(parents=True, exist_ok=True)


async def create(chat_id: str, prompt: str, webhook_secret: str) -> dict:
    record = {
        "id": f"launch_{uuid.uuid4().hex[:12]}",
        "chat_id": chat_id,
        "title": prompt.strip().splitlines()[0][:80] or "coder task",
        "prompt": prompt,
        "state": "creating",
        "webhook_secret": webhook_secret,
        "webhook_seq": 0,
        "completion_delivered": False,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    await save(record)
    return record


async def finish_create(task_id: str, created: dict) -> dict:
    """Add control-plane ids without regressing an early webhook state."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM fab_tasks WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                raise KeyError(task_id)
            record = _data(row["data"])
            record["task_id"] = created["task_id"]
            record["session_id"] = created["session_id"]
            if record.get("state") == "creating":
                record["state"] = created["state"]
            await conn.execute(
                "UPDATE fab_tasks SET data = $2::jsonb WHERE id = $1",
                task_id,
                json.dumps(record, separators=(",", ":")),
            )
            return record
    path = _path(task_id)
    with _lock:
        if not path.exists():
            raise KeyError(task_id)
        record = json.loads(path.read_text())
        record["task_id"] = created["task_id"]
        record["session_id"] = created["session_id"]
        if record.get("state") == "creating":
            record["state"] = created["state"]
        path.write_text(json.dumps(record, separators=(",", ":")))
        return record


async def save(record: dict) -> None:
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO fab_tasks (id, chat_id, data) VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            record["id"],
            record["chat_id"],
            json.dumps(record, separators=(",", ":")),
        )
        return
    path = _path(record["id"])
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, separators=(",", ":")))


async def get(task_id: str) -> dict | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM fab_tasks WHERE id = $1", task_id
        )
        return _data(row["data"]) if row is not None else None
    path = _path(task_id)
    with _lock:
        return json.loads(path.read_text()) if path.exists() else None


async def list_for_chat(chat_id: str) -> list[dict]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM fab_tasks WHERE chat_id = $1 ORDER BY created_at", chat_id
        )
        return [_data(row["data"]) for row in rows]
    with _lock:
        found = []
        for path in (store.data_dir() / "tasks").glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("chat_id") == chat_id:
                found.append(record)
        return sorted(found, key=lambda record: record.get("created_at", ""))


def _data(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else raw


def _path(task_id: str):
    return store.data_dir() / "tasks" / f"{urllib.parse.quote(task_id, safe='')}.json"
