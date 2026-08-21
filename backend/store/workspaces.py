"""Cross-instance lock for provisioning one shared devbox per chat."""

import asyncio
import contextlib

import store

_locks: dict[str, asyncio.Lock] = {}


@contextlib.asynccontextmanager
async def provision(chat_id: str):
    """Serialize the rare box/taskset creation path for one chat."""
    if store.use_postgres():
        from store import db

        async with (await db.pool()).acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtext($1))", chat_id)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", chat_id)
        return

    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        yield
