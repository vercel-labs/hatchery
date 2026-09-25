"""Agents: shared, named workers with repos and resources.

Rows are the models.Agent json verbatim — the store adds no schema of its
own beyond the id. The id is an agentmesh slug (a Git path segment and DNS
label) and never changes; the name is a display label. default() seeds
hatchery's own agent on first use so a fresh deployment has somewhere to land
chats.
"""

import datetime
import json
import re
import secrets
import unicodedata
import urllib.parse

import pydantic

from hatchery import models
from hatchery import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_agents (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DEFAULT_ID = "hatchery"
ACCENT_COLORS: tuple[models.AccentColor, ...] = (
    "blue-700",
    "red-700",
    "amber-700",
    "green-700",
    "teal-700",
    "purple-700",
    "pink-700",
)

_schema_ready = False


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from hatchery.store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "agents").mkdir(parents=True, exist_ok=True)


class Taken(Exception):
    """An agent with this id already exists."""


def validate_slug(value: str) -> str:
    """A portable lowercase name usable as a path component and Git ref segment."""
    # copied from agentmesh config.validate_slug
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError("id must be 1-63 lowercase letters, digits, or interior hyphens")
    return value


async def create(
    name: str,
    agent_id: str | None = None,
    color: models.AccentColor | None = None,
) -> models.Agent:
    """Create an empty agent. Without an id, it is slugified from the name.

    Raises ValueError for an invalid id and Taken when the id already exists.
    """
    if agent_id is None:
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        agent_id = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:63].strip("-")
    agent = models.Agent(
        id=validate_slug(agent_id),
        name=name,
        color=color or secrets.choice(ACCENT_COLORS),
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    if store.use_postgres():
        from hatchery.store import db

        result = await (await db.pool()).execute(
            "INSERT INTO hatchery_agents (id, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            agent.id,
            agent.model_dump_json(),
        )
        if result == "INSERT 0 0":
            raise Taken(agent.id)
        return agent
    path = _path(agent.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(agent.model_dump_json())
    except FileExistsError as error:
        raise Taken(agent.id) from error
    return agent


async def save(agent: models.Agent) -> models.Agent:
    """Insert or replace one agent."""
    if store.use_postgres():
        from hatchery.store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_agents (id, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            agent.id,
            agent.model_dump_json(),
        )
        return agent
    path = _path(agent.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(agent.model_dump_json(), encoding="utf-8")
    return agent


async def delete(agent_id: str) -> bool:
    """Delete one agent, returning whether it existed."""
    if store.use_postgres():
        from hatchery.store import db

        result = await (await db.pool()).execute(
            "DELETE FROM hatchery_agents WHERE id = $1", agent_id
        )
        return result != "DELETE 0"
    path = _path(agent_id)
    if not path.exists():
        return False
    path.unlink()
    return True


async def get(agent_id: str) -> models.Agent | None:
    if store.use_postgres():
        from hatchery.store import db

        row = await (await db.pool()).fetchrow("SELECT data FROM hatchery_agents WHERE id = $1", agent_id)
        return models.Agent.model_validate_json(_json(row["data"])) if row is not None else None
    path = _path(agent_id)
    if not path.exists():
        return None
    try:
        return models.Agent.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


async def list_all() -> list[models.Agent]:
    """All agents, oldest first (a stable sidebar order)."""
    if store.use_postgres():
        from hatchery.store import db

        rows = await (await db.pool()).fetch("SELECT data FROM hatchery_agents ORDER BY created_at")
        return [models.Agent.model_validate_json(_json(row["data"])) for row in rows]
    found = []
    for path in sorted((store.data_dir() / "agents").glob("*.json")):
        agent = await get(urllib.parse.unquote(path.stem))
        if agent is not None:
            found.append(agent)
    found.sort(key=lambda s: s.created_at)
    return found


async def default() -> models.Agent:
    """Default agent, created on first call."""
    existing = await get(DEFAULT_ID)
    if existing is not None:
        return existing
    return await save(
        models.Agent(
            id=DEFAULT_ID,
            name="hatchery",
            color=secrets.choice(ACCENT_COLORS),
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
    )


def _json(raw) -> str:
    # asyncpg returns jsonb as str unless a codec is installed
    return raw if isinstance(raw, str) else json.dumps(raw)


def _path(agent_id: str):
    return store.data_dir() / "agents" / f"{urllib.parse.quote(agent_id, safe='')}.json"
