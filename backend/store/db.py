"""Postgres pool, keyed by the running event loop (seal's backend/db.py).

The FastAPI service runs on one long-lived ASGI loop, so a cached pool is safe
there. The workflow worker is different: a warm process handles each queue
message on a fresh event loop and closes it afterwards. An asyncpg pool is
bound to the loop that created it, so a globally cached pool poisons the next
invocation. We key the cache by the running loop and rebuild when it changes;
the stale pool is dropped for GC (closing it would just re-raise "Event loop
is closed").
"""

import asyncio
import os

import asyncpg

_pool: asyncpg.Pool | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None


async def pool() -> asyncpg.Pool:
    """Return the pool bound to the running loop, creating it on first call."""
    global _pool, _pool_loop
    loop = asyncio.get_running_loop()
    if _pool is not None and _pool_loop is loop:
        return _pool
    # min_size=0: connections are made lazily so a short-lived worker loop
    # doesn't open connections it never uses.
    _pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=0,
        max_inactive_connection_lifetime=60.0,
    )
    _pool_loop = loop
    return _pool
