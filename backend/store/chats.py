"""Chats and the channel bindings that feed them.

A chat is one conversation in a space, visible in the UI. A binding maps a
channel-scoped token ("slack:C1:1712.001", "github:owner/repo:issue:7") to
its chat, so the same conversation is reachable from slack, github, and the
UI at once. Single-owner: one chat per token, enforced by an atomic claim
(postgres: INSERT .. ON CONFLICT DO NOTHING; local: one lock). dedupe gives
webhooks durable replay protection.

Chat rows are the models.Chat json verbatim, plus a nullable space_id column
for filtering. Postgres when DATABASE_URL is set, otherwise json files under
HATCHERY_DATA_DIR. Locking mirrors store.events.
"""

import datetime
import json
import threading
import urllib.parse
import uuid

import pydantic

import models
import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_chats (
    id         TEXT PRIMARY KEY,
    space_id   TEXT,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE hatchery_chats ALTER COLUMN space_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS hatchery_bindings (
    token      TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    channel    TEXT NOT NULL,
    state      JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hatchery_bindings_chat ON hatchery_bindings (chat_id);

CREATE TABLE IF NOT EXISTS hatchery_dedupe (
    key        TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_lock = threading.Lock()
_schema_ready = False


class Binding(pydantic.BaseModel):
    token: str
    chat_id: str
    channel: str
    state: dict = {}


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "chats").mkdir(parents=True, exist_ok=True)


async def create(space_id: str | None, title: str, trigger: str = "ui") -> models.Chat:
    chat = models.Chat(
        id=f"chat_{uuid.uuid4().hex[:12]}",
        space_id=space_id,
        title=title,
        trigger=trigger,
        created_at=_now(),
    )
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_chats (id, space_id, data) VALUES ($1, $2, $3::jsonb)",
            chat.id,
            chat.space_id,
            chat.model_dump_json(),
        )
        return chat
    with _lock:
        _write_chat(chat)
        return chat


async def get(chat_id: str) -> models.Chat | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow("SELECT data FROM hatchery_chats WHERE id = $1", chat_id)
        return _chat(row["data"]) if row is not None else None
    with _lock:
        return _read_chat(chat_id)


async def list_all() -> list[models.Chat]:
    """Every chat, newest first (the sidebar list)."""
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch("SELECT data FROM hatchery_chats ORDER BY created_at DESC")
        return [_chat(row["data"]) for row in rows]
    with _lock:
        chats = [_read_chat(urllib.parse.unquote(p.stem)) for p in (store.data_dir() / "chats").glob("*.json")]
        found = [c for c in chats if c is not None]
        found.sort(key=lambda c: c.created_at, reverse=True)
        return found


async def assign_space(chat_id: str, space_id: str) -> models.Chat | None:
    chat = await get(chat_id)
    if chat is None:
        return None
    chat.space_id = space_id
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "UPDATE hatchery_chats SET space_id = $2, data = $3::jsonb WHERE id = $1",
            chat_id,
            space_id,
            chat.model_dump_json(),
        )
        return chat
    with _lock:
        _write_chat(chat)
        return chat


async def finish(chat_id: str, status: str, artifact: str | None = None) -> models.Chat | None:
    """Record a chat's worker status and optional terminal artifact."""
    chat = await get(chat_id)
    if chat is None:
        return None
    chat.status = status
    chat.artifact = artifact
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "UPDATE hatchery_chats SET data = $2::jsonb WHERE id = $1", chat_id, chat.model_dump_json()
        )
        return chat
    with _lock:
        _write_chat(chat)
        return chat


async def claim(
    token: str, channel: str, space_id: str, title: str, state: dict
) -> tuple[models.Chat, bool]:
    """Atomically map a channel token to its owning chat.

    Creates the chat (and binding) if the token is unowned, otherwise returns
    the existing owner with the binding state merged in. This is what stops
    two concurrent webhooks from both creating a chat for the same
    conversation. Returns (chat, created).
    """
    candidate = models.Chat(
        id=f"chat_{uuid.uuid4().hex[:12]}",
        space_id=space_id,
        title=title,
        trigger=token,
        created_at=_now(),
    )
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO hatchery_bindings (token, chat_id, channel, state) "
                "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (token) DO NOTHING RETURNING chat_id",
                token,
                candidate.id,
                channel,
                json.dumps(state),
            )
            if row is not None:
                await conn.execute(
                    "INSERT INTO hatchery_chats (id, space_id, data) VALUES ($1, $2, $3::jsonb)",
                    candidate.id,
                    candidate.space_id,
                    candidate.model_dump_json(),
                )
                return candidate, True
            await conn.execute(
                "UPDATE hatchery_bindings SET state = state || $2::jsonb WHERE token = $1",
                token,
                json.dumps(state),
            )
            owner = await conn.fetchrow(
                "SELECT c.data FROM hatchery_chats c "
                "JOIN hatchery_bindings b ON b.chat_id = c.id WHERE b.token = $1",
                token,
            )
            return _chat(owner["data"]), False

    with _lock:
        bindings_ = _read_bindings()
        existing = bindings_.get(token)
        if existing is not None:
            existing["state"] = {**existing.get("state", {}), **state}
            _write_bindings(bindings_)
            owner = _read_chat(existing["chat_id"])
            if owner is not None:
                return owner, False
        bindings_[token] = {"chat_id": candidate.id, "channel": channel, "state": dict(state)}
        _write_bindings(bindings_)
        _write_chat(candidate)
        return candidate, True


async def bindings(chat_id: str) -> list[Binding]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch("SELECT * FROM hatchery_bindings WHERE chat_id = $1", chat_id)
        return [
            Binding(
                token=row["token"],
                chat_id=row["chat_id"],
                channel=row["channel"],
                state=json.loads(row["state"]) if isinstance(row["state"], str) else row["state"],
            )
            for row in rows
        ]
    with _lock:
        return [
            Binding(token=token, chat_id=data["chat_id"], channel=data["channel"], state=data.get("state", {}))
            for token, data in _read_bindings().items()
            if data["chat_id"] == chat_id
        ]


async def dedupe(key: str) -> bool:
    """True the first time a key is seen, False on replays."""
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "INSERT INTO hatchery_dedupe (key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", key
        )
        return row is not None
    with _lock:
        path = store.data_dir() / "dedupe.json"
        seen = json.loads(path.read_text()) if path.exists() else []
        if key in seen:
            return False
        seen.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen))
        return True


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _chat(raw) -> models.Chat:
    # asyncpg returns jsonb as str unless a codec is installed
    return models.Chat.model_validate_json(raw if isinstance(raw, str) else json.dumps(raw))


def _read_chat(chat_id: str) -> models.Chat | None:
    path = store.data_dir() / "chats" / f"{urllib.parse.quote(chat_id, safe='')}.json"
    if not path.exists():
        return None
    try:
        return models.Chat.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


def _write_chat(chat: models.Chat) -> None:
    path = store.data_dir() / "chats" / f"{urllib.parse.quote(chat.id, safe='')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(chat.model_dump_json(), encoding="utf-8")


def _read_bindings() -> dict:
    path = store.data_dir() / "bindings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_bindings(bindings_: dict) -> None:
    path = store.data_dir() / "bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bindings_))
