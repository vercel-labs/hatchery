"""Projects: the app-level grouping — repos, chats, and one memory file.

memory is one text blob per project capturing current state and direction;
the UI edits it and the agent reads it into every turn's system prompt.
repos is a list of "owner/repo" names; inbound github chats route to the
project that lists their repository, everything else lands in the default
project.

Postgres when DATABASE_URL is set, otherwise one json file per project.
Locking mirrors store.events (threading.Lock, sync file I/O only).
"""

import datetime
import json
import threading
import typing
import urllib.parse
import uuid

import pydantic

import store

DEFAULT_NAME = "default"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS factory_projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    memory     TEXT NOT NULL DEFAULT '',
    repos      JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_lock = threading.Lock()
_schema_ready = False


class Project(pydantic.BaseModel):
    id: str
    name: str
    memory: str = ""
    repos: list[str] = []
    created_at: str
    updated_at: str


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        _root().mkdir(parents=True, exist_ok=True)


async def list_projects() -> list[Project]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch("SELECT * FROM factory_projects ORDER BY created_at")
        return [_from_row(row) for row in rows]
    with _lock:
        projects = [_read_local(path.stem) for path in _root().glob("*.json")]
        found = [p for p in projects if p is not None]
        found.sort(key=lambda p: p.created_at)
        return found


async def get(project_id: str) -> Project | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow("SELECT * FROM factory_projects WHERE id = $1", project_id)
        return _from_row(row) if row is not None else None
    with _lock:
        return _read_local(project_id)


async def create(name: str) -> Project:
    """Create a project, or return the existing one with that name."""
    candidate = Project(id=f"prj_{uuid.uuid4().hex[:12]}", name=name, created_at=_now(), updated_at=_now())
    if store.use_postgres():
        from store import db

        async with (await db.pool()).acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO factory_projects (id, name) VALUES ($1, $2) "
                "ON CONFLICT (name) DO NOTHING RETURNING *",
                candidate.id,
                name,
            ) or await conn.fetchrow("SELECT * FROM factory_projects WHERE name = $1", name)
        return _from_row(row)
    with _lock:
        for path in _root().glob("*.json"):
            existing = _read_local(path.stem)
            if existing is not None and existing.name == name:
                return existing
        _write_local(candidate)
        return candidate


async def get_default() -> Project:
    """The project chats land in when nothing routes them elsewhere."""
    return await create(DEFAULT_NAME)


async def for_repo(full_name: str) -> Project | None:
    """The project whose repos list contains "owner/repo", if any."""
    for project in await list_projects():
        if full_name in project.repos:
            return project
    return None


async def set_memory(project_id: str, memory: str) -> Project | None:
    return await _update(project_id, memory=memory)


async def set_repos(project_id: str, repos: list[str]) -> Project | None:
    return await _update(project_id, repos=repos)


async def _update(project_id: str, **fields: typing.Any) -> Project | None:
    if store.use_postgres():
        from store import db

        sets, values = [], []
        for offset, (key, value) in enumerate(fields.items()):
            sets.append(f"{key} = ${offset + 2}" + ("::jsonb" if key == "repos" else ""))
            values.append(json.dumps(value) if key == "repos" else value)
        row = await (await db.pool()).fetchrow(
            f"UPDATE factory_projects SET {', '.join(sets)}, updated_at = now() WHERE id = $1 RETURNING *",
            project_id,
            *values,
        )
        return _from_row(row) if row is not None else None
    with _lock:
        existing = _read_local(project_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={**fields, "updated_at": _now()})
        _write_local(updated)
        return updated


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _root():
    return store.data_dir() / "projects"


def _read_local(project_id: str) -> Project | None:
    path = _root() / f"{urllib.parse.quote(project_id, safe='')}.json"
    if not path.exists():
        return None
    try:
        return Project.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


def _write_local(project: Project) -> None:
    _root().mkdir(parents=True, exist_ok=True)
    path = _root() / f"{urllib.parse.quote(project.id, safe='')}.json"
    path.write_text(project.model_dump_json(), encoding="utf-8")


def _from_row(row: typing.Any) -> Project:
    repos = row["repos"]
    return Project(
        id=row["id"],
        name=row["name"],
        memory=row["memory"],
        repos=json.loads(repos) if isinstance(repos, str) else repos,
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
