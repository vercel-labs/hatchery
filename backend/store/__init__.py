"""store: everything durable — spaces, chats, and their event streams.

Each module is one entity with one postgres and one local-files backend,
selected by DATABASE_URL (seal's pattern). Local files live under
HATCHERY_DATA_DIR (default backend/.data) so tests and dev need no database.
"""

import os
import pathlib


def data_dir() -> pathlib.Path:
    configured = os.environ.get("HATCHERY_DATA_DIR")
    if configured:
        return pathlib.Path(configured)
    return pathlib.Path(__file__).resolve().parents[1] / ".data"


def use_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


async def ensure_ready() -> None:
    """Prepare all stores (idempotent DDL / local dirs). Call once at startup."""
    from store import chats, devboxes, events, spaces, subagents

    await spaces.ensure_ready()
    await chats.ensure_ready()
    await events.ensure_ready()
    await devboxes.ensure_ready()
    await subagents.ensure_ready()
