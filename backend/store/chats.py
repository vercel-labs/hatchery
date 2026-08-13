"""Chats and the channel bindings that feed them.

A chat is one conversation in a project, visible in the UI. A binding maps a
channel-local address ("slack:C1:1712.001", "github:repo:42:issue:7") to its
chat, so the same conversation is reachable from slack, github, and the UI at
once. Single-owner: one live chat per token, enforced by an atomic claim
(postgres: INSERT .. ON CONFLICT DO NOTHING; local: one lock). dedupe gives
webhooks durable replay protection.

Postgres when DATABASE_URL is set, otherwise json files under
FACTORY_DATA_DIR. Locking mirrors store.events.
"""

import datetime
import json
import threading
import typing
import urllib.parse
import uuid

import pydantic

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS factory_chats (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factory_bindings (
    token      TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    channel    TEXT NOT NULL,
    state      JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS factory_bindings_chat ON factory_bindings (chat_id);

CREATE TABLE IF NOT EXISTS factory_dedupe (
    key        TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_lock = threading.Lock()
_schema_ready = False


class Chat(pydantic.BaseModel):
    id: str
    project_id: str
    title: str
    status: typing.Literal["active", "archived"] = "active"
    created_at: str
    updated_at: str


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
        _root().mkdir(parents=True, exist_ok=True)


async def create(project_id: str, title: str) -> Chat:
    chat = Chat(
        id=f"cht_{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        title=title,
        created_at=_now(),
        updated_at=_now(),
    )
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO factory_chats (id, project_id, title) VALUES ($1, $2, $3)",
            chat.id,
            project_id,
            title,
        )
        return chat
    with _lock:
        _write_chat(chat)
        return chat


async def get(chat_id: str) -> Chat | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow("SELECT * FROM factory_chats WHERE id = $1", chat_id)
        return _chat_from_row(row) if row is not None else None
    with _lock:
        return _read_chat(chat_id)


async def list_for_project(project_id: str) -> list[Chat]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT * FROM factory_chats WHERE project_id = $1 ORDER BY updated_at DESC", project_id
        )
        return [_chat_from_row(row) for row in rows]
    with _lock:
        chats = [_read_chat(path.stem) for path in (_root() / "chats").glob("*.json")]
        found = [c for c in chats if c is not None and c.project_id == project_id]
        found.sort(key=lambda c: c.updated_at, reverse=True)
        return found


async def set_status(chat_id: str, status: typing.Literal["active", "archived"]) -> Chat | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "UPDATE factory_chats SET status = $2, updated_at = now() WHERE id = $1 RETURNING *",
            chat_id,
            status,
        )
        return _chat_from_row(row) if row is not None else None
    with _lock:
        chat = _read_chat(chat_id)
        if chat is None:
            return None
        updated = chat.model_copy(update={"status": status, "updated_at": _now()})
        _write_chat(updated)
        return updated


async def touch(chat_id: str) -> None:
    """Bump updated_at on new activity so project chat lists sort by recency."""
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "UPDATE factory_chats SET updated_at = now() WHERE id = $1", chat_id
        )
        return
    with _lock:
        chat = _read_chat(chat_id)
        if chat is not None:
            _write_chat(chat.model_copy(update={"updated_at": _now()}))


async def claim(token: str, channel: str, project_id: str, title: str, state: dict) -> tuple[Chat, bool]:
    """Atomically map a channel token to its owning chat.

    Creates the chat (and binding) if the token is unowned, otherwise returns
    the existing owner with the binding state merged in. This is what stops two
    concurrent webhooks from both creating a chat for the same conversation.
    Returns (chat, created).
    """
    candidate = Chat(
        id=f"cht_{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        title=title,
        created_at=_now(),
        updated_at=_now(),
    )
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO factory_bindings (token, chat_id, channel, state) "
                "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (token) DO NOTHING RETURNING chat_id",
                token,
                candidate.id,
                channel,
                json.dumps(state),
            )
            if row is not None:
                await conn.execute(
                    "INSERT INTO factory_chats (id, project_id, title) VALUES ($1, $2, $3)",
                    candidate.id,
                    project_id,
                    title,
                )
                return candidate, True
            await conn.execute(
                "UPDATE factory_bindings SET state = state || $2::jsonb WHERE token = $1",
                token,
                json.dumps(state),
            )
            owner = await conn.fetchrow(
                "SELECT c.* FROM factory_chats c "
                "JOIN factory_bindings b ON b.chat_id = c.id WHERE b.token = $1",
                token,
            )
            return _chat_from_row(owner), False

    with _lock:
        bindings = _read_bindings()
        existing = bindings.get(token)
        if existing is not None:
            existing["state"] = {**existing.get("state", {}), **state}
            _write_bindings(bindings)
            owner = _read_chat(existing["chat_id"])
            if owner is not None:
                return owner, False
        bindings[token] = {"chat_id": candidate.id, "channel": channel, "state": dict(state)}
        _write_bindings(bindings)
        _write_chat(candidate)
        return candidate, True


async def bindings(chat_id: str) -> list[Binding]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT * FROM factory_bindings WHERE chat_id = $1", chat_id
        )
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
            "INSERT INTO factory_dedupe (key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", key
        )
        return row is not None
    with _lock:
        path = _root() / "dedupe.json"
        seen = json.loads(path.read_text()) if path.exists() else []
        if key in seen:
            return False
        seen.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen))
        return True


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _root():
    return store.data_dir()


def _read_chat(chat_id: str) -> Chat | None:
    path = _root() / "chats" / f"{urllib.parse.quote(chat_id, safe='')}.json"
    if not path.exists():
        return None
    try:
        return Chat.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


def _write_chat(chat: Chat) -> None:
    path = _root() / "chats" / f"{urllib.parse.quote(chat.id, safe='')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(chat.model_dump_json(), encoding="utf-8")


def _read_bindings() -> dict:
    path = _root() / "bindings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_bindings(bindings: dict) -> None:
    path = _root() / "bindings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bindings))


def _chat_from_row(row: typing.Any) -> Chat:
    return Chat(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
