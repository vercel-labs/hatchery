"""Append-only event streams, keyed by (stream_id, namespace).

seal's storage shape: the stream is the source of truth, snapshots are just
streams whose tail wins. One stream per chat per concern:

- (chat_id, "messages"): the transcript, one model message per event. The UI
  loads it on open, turns derive their history from it, channel inbound
  appends to it.
- (chat_id, "worker"): snapshots of the chat's shared devbox record (box and
  taskset ids). Individual launches and PTY sessions live in store.tasks.

Postgres when DATABASE_URL is set, otherwise one jsonl file per stream under
FAB_DATA_DIR. The jsonl locks are threading.Locks on purpose: a workflow
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
CREATE TABLE IF NOT EXISTS fab_streams (
    stream_id  TEXT NOT NULL,
    ns         TEXT NOT NULL,
    tail_index INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, ns)
);

CREATE TABLE IF NOT EXISTS fab_events (
    stream_id  TEXT NOT NULL,
    ns         TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, ns, idx)
);
"""

_locks: dict[tuple[str, str], threading.Lock] = {}
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
        (store.data_dir() / "events").mkdir(parents=True, exist_ok=True)


async def append(stream_id: str, ns: str, data: dict[str, typing.Any]) -> int:
    """Append one event and return its 0-based index."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO fab_streams (stream_id, ns) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                stream_id,
                ns,
            )
            row = await conn.fetchrow(
                "UPDATE fab_streams SET tail_index = tail_index + 1, updated_at = now() "
                "WHERE stream_id = $1 AND ns = $2 RETURNING tail_index - 1 AS idx",
                stream_id,
                ns,
            )
            index = int(row["idx"])
            await conn.execute(
                "INSERT INTO fab_events (stream_id, ns, idx, data) VALUES ($1, $2, $3, $4::jsonb)",
                stream_id,
                ns,
                index,
                json.dumps(data, separators=(",", ":")),
            )
            return index

    path = _path(stream_id, ns)
    with _lock(stream_id, ns):
        index = sum(1 for line in path.read_text().splitlines() if line) if path.exists() else 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, separators=(",", ":")) + "\n")
        return index


async def read(
    stream_id: str, ns: str, start_index: int = 0
) -> list[tuple[int, dict[str, typing.Any]]]:
    """Return (index, data) pairs with index >= start_index."""
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT idx, data FROM fab_events "
            "WHERE stream_id = $1 AND ns = $2 AND idx >= $3 ORDER BY idx",
            stream_id,
            ns,
            start_index,
        )
        return [(int(row["idx"]), _data(row["data"])) for row in rows]

    path = _path(stream_id, ns)
    with _lock(stream_id, ns):
        if not path.exists():
            return []
        return [
            (index, json.loads(line))
            for index, line in enumerate(path.read_text().splitlines())
            if line and index >= start_index
        ]


async def tail(stream_id: str, ns: str) -> dict[str, typing.Any] | None:
    """Data of the last event, None when the stream is empty."""
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM fab_events WHERE stream_id = $1 AND ns = $2 "
            "ORDER BY idx DESC LIMIT 1",
            stream_id,
            ns,
        )
        return _data(row["data"]) if row is not None else None

    path = _path(stream_id, ns)
    with _lock(stream_id, ns):
        if not path.exists():
            return None
        lines = [line for line in path.read_text().splitlines() if line]
        return json.loads(lines[-1]) if lines else None


def _data(raw: typing.Any) -> dict[str, typing.Any]:
    # asyncpg returns jsonb as str unless a codec is installed
    return json.loads(raw) if isinstance(raw, str) else raw


def _path(stream_id: str, ns: str):
    quote = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
    return store.data_dir() / "events" / f"{quote(stream_id)}.{quote(ns)}.jsonl"


def _lock(stream_id: str, ns: str) -> threading.Lock:
    # setdefault is an atomic get-or-create, so racing threads share one lock
    return _locks.setdefault((stream_id, ns), threading.Lock())
