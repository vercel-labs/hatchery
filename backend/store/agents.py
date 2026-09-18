"""Agents: durable identity and configuration."""

import datetime
import json
import pathlib
import secrets
import urllib.parse
import uuid

import pydantic

import models
import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_agents (
    id         TEXT PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DEFAULT_ID = "agt_hatchery"
ACCENT_COLORS: tuple[models.AccentColor, ...] = (
    "blue-700",
    "red-700",
    "amber-700",
    "green-700",
    "teal-700",
    "purple-700",
    "pink-700",
)
_DEFAULT_ABOUT = (
    "An agent for repositories, instructions, and reference material.\n\n"
    "## Conventions\n\n"
    "Keep changes small and reviewable. Prefer a report over a pr when uncertain."
)

_schema_ready = False


class SlugExists(Exception):
    pass


class SlugImmutable(Exception):
    pass


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "agents").mkdir(parents=True, exist_ok=True)


async def create(
    name: str,
    slug: str | None = None,
    color: models.AccentColor | None = None,
) -> models.Agent:
    """Create an agent with a stable, unique storage slug."""
    base = slug or slug_for(name)
    candidate = base
    suffix = 2
    while await get_by_slug(candidate) is not None:
        tail = f"-{suffix}"
        candidate = f"{base[: 63 - len(tail)].rstrip('-')}{tail}"
        suffix += 1
    return await save(
        models.Agent(
            id=f"agt_{uuid.uuid4().hex[:12]}",
            slug=candidate,
            name=name,
            color=color or secrets.choice(ACCENT_COLORS),
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
    )


async def save(agent: models.Agent) -> models.Agent:
    """Insert or replace an agent, but never change its slug."""
    await ensure_ready()
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT slug FROM hatchery_agents WHERE id = $1 FOR UPDATE", agent.id
            )
            if row is not None and row["slug"] != agent.slug:
                raise SlugImmutable(agent.id)
            try:
                await connection.execute(
                    "INSERT INTO hatchery_agents (id, slug, data) VALUES ($1, $2, $3::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                    agent.id,
                    agent.slug,
                    agent.model_dump_json(),
                )
            except Exception as error:
                if getattr(error, "sqlstate", None) == "23505":
                    raise SlugExists(agent.slug) from error
                raise
        return agent

    existing = await get(agent.id)
    if existing is not None and existing.slug != agent.slug:
        raise SlugImmutable(agent.id)
    for other in await list_all():
        if other.id != agent.id and other.slug == agent.slug:
            raise SlugExists(agent.slug)
    path = _path(agent.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(agent.model_dump_json(), encoding="utf-8")
    return agent


async def delete(agent_id: str) -> bool:
    if store.use_postgres():
        from store import db

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
    await ensure_ready()
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_agents WHERE id = $1", agent_id
        )
        return models.Agent.model_validate_json(_json(row["data"])) if row else None
    path = _path(agent_id)
    if not path.exists():
        return None
    try:
        return models.Agent.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


async def get_by_slug(slug: str) -> models.Agent | None:
    await ensure_ready()
    models.Agent.valid_slug(slug)
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_agents WHERE slug = $1", slug
        )
        return models.Agent.model_validate_json(_json(row["data"])) if row else None
    return next((agent for agent in await list_all() if agent.slug == slug), None)


async def list_all() -> list[models.Agent]:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_agents ORDER BY created_at"
        )
        return [models.Agent.model_validate_json(_json(row["data"])) for row in rows]
    found = []
    for path in sorted((store.data_dir() / "agents").glob("*.json")):
        agent = await get(urllib.parse.unquote(path.stem))
        if agent is not None:
            found.append(agent)
    found.sort(key=lambda item: item.created_at)
    return found


async def default() -> models.Agent:
    existing = await get(DEFAULT_ID)
    if existing is not None:
        return existing
    return await save(
        models.Agent(
            id=DEFAULT_ID,
            slug="hatchery",
            name="hatchery",
            about=_DEFAULT_ABOUT,
            color=secrets.choice(ACCENT_COLORS),
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
    )


def slug_for(name: str) -> str:
    slug = "-".join(filter(None, "".join(
        character.lower() if character.isascii() and character.isalnum() else " "
        for character in name
    ).split()))[:63].strip("-")
    return slug or "agent"


def _json(raw) -> str:
    return raw if isinstance(raw, str) else json.dumps(raw)


def _path(agent_id: str) -> pathlib.Path:
    return store.data_dir() / "agents" / f"{urllib.parse.quote(agent_id, safe='')}.json"
