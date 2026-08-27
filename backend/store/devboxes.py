"""Durable devboxes, owned by a chat and shared by its subagents."""

import datetime
import json
import threading
import urllib.parse
import uuid

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_devboxes (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_devboxes_chat ON hatchery_devboxes (chat_id, created_at);
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
        (store.data_dir() / "devboxes").mkdir(parents=True, exist_ok=True)


async def create(chat_id: str, title: str, repos: list[str]) -> dict:
    record = {
        "id": f"devbox_{uuid.uuid4().hex[:12]}",
        "chat_id": chat_id,
        "title": title.strip()[:80] or "devbox",
        "repos": list(repos),
        "state": "creating",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    await save(record)
    return record


async def save(record: dict) -> None:
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_devboxes (id, chat_id, data) VALUES ($1, $2, $3::jsonb) "
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


async def get(devbox_id: str) -> dict | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_devboxes WHERE id = $1", devbox_id
        )
        return _data(row["data"]) if row is not None else None
    path = _path(devbox_id)
    with _lock:
        return json.loads(path.read_text()) if path.exists() else None


async def delete(devbox_id: str) -> bool:
    if store.use_postgres():
        from store import db

        result = await (await db.pool()).execute(
            "DELETE FROM hatchery_devboxes WHERE id = $1", devbox_id
        )
        return result != "DELETE 0"
    path = _path(devbox_id)
    with _lock:
        if not path.exists():
            return False
        path.unlink()
        return True


async def list_for_chat(chat_id: str) -> list[dict]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_devboxes WHERE chat_id = $1 ORDER BY created_at", chat_id
        )
        return [_data(row["data"]) for row in rows]
    with _lock:
        found = []
        for path in (store.data_dir() / "devboxes").glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("chat_id") == chat_id:
                found.append(record)
        return sorted(found, key=lambda record: record.get("created_at", ""))


def _data(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else raw


def _path(devbox_id: str):
    return store.data_dir() / "devboxes" / f"{urllib.parse.quote(devbox_id, safe='')}.json"
