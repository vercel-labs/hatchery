"""Postgres pool, keyed by the running event loop (seal's backend/db.py).

The FastAPI service runs on one long-lived ASGI loop, so a cached pool is safe
there. A workflow worker is different: a warm process handles each queue
message on a fresh event loop and closes it afterwards. An asyncpg pool is
bound to the loop that created it, so a globally cached pool poisons the next
invocation. We key the cache by the running loop and rebuild when it changes;
the stale pool is dropped for GC (closing it would just re-raise "Event loop
is closed").
"""

import asyncio
import os
import urllib.parse

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
    # doesn't open connections it never uses. statement_cache_size=0: neon's
    # pooled url is pgbouncer in transaction mode, where cached prepared
    # statements surface as "prepared statement does not exist" flakes.
    _pool = await asyncpg.create_pool(
        dsn=_dsn(),
        min_size=0,
        max_inactive_connection_lifetime=60.0,
        statement_cache_size=0,
    )
    _pool_loop = loop
    return _pool


def _dsn() -> str:
    return _clean_dsn(os.environ["DATABASE_URL"])


def direct_dsn() -> str:
    """Direct connection for session features such as LISTEN/NOTIFY."""
    return _clean_dsn(os.environ.get("DATABASE_URL_UNPOOLED", os.environ["DATABASE_URL"]))


def _clean_dsn(value: str) -> str:
    # neon connection strings carry channel_binding=require, a libpq parameter
    # asyncpg doesn't know; it would refuse the whole url.
    parts = urllib.parse.urlsplit(value)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "channel_binding"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
