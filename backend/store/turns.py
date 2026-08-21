"""Serialize channel-driven dispatcher turns per chat."""

import asyncio
import contextlib

import store

_locks: dict[str, asyncio.Lock] = {}


@contextlib.asynccontextmanager
async def run(chat_id: str):
    """Allow one inbound dispatcher turn at a time for a chat."""
    if store.use_postgres():
        from store import db

        key = f"turn:{chat_id}"
        async with (await db.pool()).acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtext($1))", key)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", key)
        return

    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        yield
