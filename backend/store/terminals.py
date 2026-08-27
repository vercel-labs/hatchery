"""Durable manual terminal tabs, owned by a devbox."""

import datetime
import json
import threading
import urllib.parse
import uuid

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_terminals (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    devbox_id  TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_terminals_chat ON hatchery_terminals (chat_id, created_at);
CREATE INDEX IF NOT EXISTS hatchery_terminals_devbox ON hatchery_terminals (devbox_id, created_at);
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
        (store.data_dir() / "terminals").mkdir(parents=True, exist_ok=True)


async def create(chat_id: str, devbox_id: str, title: str) -> dict:
    record = {
        "id": f"terminal_{uuid.uuid4().hex[:12]}",
        "chat_id": chat_id,
        "devbox_id": devbox_id,
        "title": title.strip()[:80] or "bash",
        "session_id": None,
        "state": "creating",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    await save(record)
    return record


async def save(record: dict) -> None:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_terminals (id, chat_id, devbox_id, data) "
            "VALUES ($1, $2, $3, $4::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            record["id"],
            record["chat_id"],
            record["devbox_id"],
            json.dumps(record, separators=(",", ":")),
        )
        return
    path = _path(record["id"])
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, separators=(",", ":")))


async def get(terminal_id: str) -> dict | None:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_terminals WHERE id = $1", terminal_id
        )
        return _data(row["data"]) if row is not None else None
    path = _path(terminal_id)
    with _lock:
        return json.loads(path.read_text()) if path.exists() else None


async def delete(terminal_id: str) -> bool:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        result = await (await db.pool()).execute(
            "DELETE FROM hatchery_terminals WHERE id = $1", terminal_id
        )
        return result != "DELETE 0"
    path = _path(terminal_id)
    with _lock:
        if not path.exists():
            return False
        path.unlink()
        return True


async def delete_for_devbox(devbox_id: str) -> None:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "DELETE FROM hatchery_terminals WHERE devbox_id = $1", devbox_id
        )
        return
    for record in await list_for_devbox(devbox_id):
        await delete(record["id"])


async def list_for_devbox(devbox_id: str) -> list[dict]:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_terminals WHERE devbox_id = $1 ORDER BY created_at",
            devbox_id,
        )
        return [_data(row["data"]) for row in rows]
    with _lock:
        found = []
        for path in (store.data_dir() / "terminals").glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("devbox_id") == devbox_id:
                found.append(record)
        return sorted(found, key=lambda record: record.get("created_at", ""))


async def list_for_chat(chat_id: str) -> list[dict]:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_terminals WHERE chat_id = $1 ORDER BY created_at",
            chat_id,
        )
        return [_data(row["data"]) for row in rows]
    with _lock:
        found = []
        for path in (store.data_dir() / "terminals").glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("chat_id") == chat_id:
                found.append(record)
        return sorted(found, key=lambda record: record.get("created_at", ""))


def _data(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else raw


def _path(terminal_id: str):
    return store.data_dir() / "terminals" / f"{urllib.parse.quote(terminal_id, safe='')}.json"
