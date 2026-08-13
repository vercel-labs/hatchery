"""Append-only event stream, one per chat.

The stream is the chat's single source of truth: user messages, assistant
replies, and progress all land here (see channels.protocol for the event
shapes). The UI tails it; channel bindings are fed from the same appends; the
agent derives its message history from it.

Postgres when DATABASE_URL is set, otherwise one jsonl file per chat under
FACTORY_DATA_DIR. The jsonl locks are threading.Locks on purpose: the workflow
worker runs each queue message on a fresh event loop (often another thread),
so an asyncio.Lock would bind to the first loop and raise on the next. They
are only ever held across synchronous file I/O — never across an await.
"""

import json
import threading
import typing
import urllib.parse

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS factory_streams (
    chat_id    TEXT PRIMARY KEY,
    tail_index INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factory_events (
    chat_id    TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, idx)
);
"""

_locks: dict[str, threading.Lock] = {}
_schema_ready = False


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            # idempotent DDL: concurrent first-callers just re-run it
            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        _path("_probe").parent.mkdir(parents=True, exist_ok=True)


async def append(chat_id: str, data: dict[str, typing.Any]) -> int:
    """Append one event and return its 0-based index."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO factory_streams (chat_id) VALUES ($1) ON CONFLICT DO NOTHING",
                chat_id,
            )
            row = await conn.fetchrow(
                "UPDATE factory_streams SET tail_index = tail_index + 1, updated_at = now() "
                "WHERE chat_id = $1 RETURNING tail_index - 1 AS idx",
                chat_id,
            )
            index = int(row["idx"])
            await conn.execute(
                "INSERT INTO factory_events (chat_id, idx, data) VALUES ($1, $2, $3::jsonb)",
                chat_id,
                index,
                json.dumps(data, separators=(",", ":")),
            )
            return index

    path = _path(chat_id)
    with _lock(chat_id):
        index = sum(1 for line in path.read_text().splitlines() if line) if path.exists() else 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, separators=(",", ":")) + "\n")
        return index


async def read(chat_id: str, start_index: int = 0) -> list[tuple[int, dict[str, typing.Any]]]:
    """Return (index, data) pairs with index >= start_index."""
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT idx, data FROM factory_events WHERE chat_id = $1 AND idx >= $2 ORDER BY idx",
            chat_id,
            start_index,
        )
        return [
            (int(row["idx"]), json.loads(row["data"]) if isinstance(row["data"], str) else row["data"])
            for row in rows
        ]

    path = _path(chat_id)
    with _lock(chat_id):
        if not path.exists():
            return []
        return [
            (index, json.loads(line))
            for index, line in enumerate(path.read_text().splitlines())
            if line and index >= start_index
        ]


async def tail_index(chat_id: str) -> int:
    """Index of the last event, -1 when the stream is empty."""
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT tail_index FROM factory_streams WHERE chat_id = $1", chat_id
        )
        return int(row["tail_index"]) - 1 if row is not None else -1

    path = _path(chat_id)
    with _lock(chat_id):
        if not path.exists():
            return -1
        return sum(1 for line in path.read_text().splitlines() if line) - 1


def _path(chat_id: str):
    return store.data_dir() / "events" / f"{urllib.parse.quote(chat_id, safe='')}.jsonl"


def _lock(chat_id: str) -> threading.Lock:
    # setdefault is an atomic get-or-create, so racing threads share one lock
    return _locks.setdefault(chat_id, threading.Lock())
