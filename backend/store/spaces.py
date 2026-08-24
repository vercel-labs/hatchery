"""Spaces: what hatchery works on (repos, goal, the about canvas).

Rows are the models.Space json verbatim — the store adds no schema of its
own beyond the id. default() seeds hatchery's own space on first use so a
fresh deployment has somewhere to land chats.
"""

import datetime
import json
import urllib.parse

import pydantic

import models
import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_spaces (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DEFAULT_ID = "spc_hatchery"

_schema_ready = False


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "spaces").mkdir(parents=True, exist_ok=True)


async def save(space: models.Space) -> models.Space:
    """Insert or replace one space."""
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_spaces (id, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            space.id,
            space.model_dump_json(),
        )
        return space
    path = _path(space.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(space.model_dump_json(), encoding="utf-8")
    return space


async def get(space_id: str) -> models.Space | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow("SELECT data FROM hatchery_spaces WHERE id = $1", space_id)
        return models.Space.model_validate_json(_json(row["data"])) if row is not None else None
    path = _path(space_id)
    if not path.exists():
        return None
    try:
        return models.Space.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


async def list_all() -> list[models.Space]:
    """All spaces, oldest first (a stable sidebar order)."""
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch("SELECT data FROM hatchery_spaces ORDER BY created_at")
        return [models.Space.model_validate_json(_json(row["data"])) for row in rows]
    found = []
    for path in sorted((store.data_dir() / "spaces").glob("*.json")):
        space = await get(urllib.parse.unquote(path.stem))
        if space is not None:
            found.append(space)
    found.sort(key=lambda s: s.created_at)
    return found


async def default() -> models.Space:
    """Hatchery's own space, created on first call."""
    existing = await get(DEFAULT_ID)
    if existing is not None:
        return existing
    return await save(
        models.Space(
            id=DEFAULT_ID,
            name="hatchery",
            goal="work on itself: respond to issues, ship prs to its own repo",
            about=(
                "# hatchery\n\n"
                "An agent deployed to the cloud, running mostly unattended. Reachable "
                "from slack, github, and this ui.\n\n"
                "## Goal\n\n"
                "Work on itself: respond to issues, ping on slack, and ship prs to its "
                "own repo.\n\n"
                "## Conventions\n\n"
                "Keep changes small and reviewable. Prefer a report over a pr when "
                "uncertain."
            ),
            repos=["anbuzin/hatchery"],
            resources=[
                models.Resource(
                    title="ai sdk for python",
                    url="https://vercel.com/docs/ai-sdk-python",
                    kind="reference",
                ),
            ],
            color="#38bdf8",
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
    )


def _json(raw) -> str:
    # asyncpg returns jsonb as str unless a codec is installed
    return raw if isinstance(raw, str) else json.dumps(raw)


def _path(space_id: str):
    return store.data_dir() / "spaces" / f"{urllib.parse.quote(space_id, safe='')}.json"
