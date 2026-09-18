"""store: everything durable — agents, threads, and their event streams.

Each entity store selects postgres or local files with DATABASE_URL. Local files
live under HATCHERY_DATA_DIR (default backend/.data).
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
    """Prepare all stores. Call once at startup."""
    from store import agents, auth, chats, events, jobs, settings
    from worker import store as workers

    await auth.ensure_ready()
    await settings.ensure_ready()
    await agents.ensure_ready()
    await chats.ensure_ready()
    await events.ensure_ready()
    await jobs.ensure_ready()
    await workers.ensure_ready()
