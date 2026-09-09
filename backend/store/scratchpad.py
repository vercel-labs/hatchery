"""One global, immutable, versioned scratchpad with optimistic writes."""

import datetime
import difflib
import json
import re
import threading

import models
import store

MAX_CONTENT_LENGTH = 32_000
MAX_HISTORY_LIMIT = 100
MAX_DIFF_LINES = 1_000

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_scratchpad_versions (
    version    INTEGER PRIMARY KEY,
    content    TEXT NOT NULL,
    actor      JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hatchery_scratchpad_state (
    singleton         BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    head_version      INTEGER NOT NULL,
    last_read_version INTEGER NOT NULL
);

INSERT INTO hatchery_scratchpad_versions (version, content, actor)
VALUES (0, '', '{"kind":"system","id":null,"name":null}'::jsonb)
ON CONFLICT (version) DO NOTHING;

INSERT INTO hatchery_scratchpad_state (singleton, head_version, last_read_version)
VALUES (TRUE, 0, 0)
ON CONFLICT (singleton) DO NOTHING;
"""

_schema_ready = False
_lock = threading.Lock()


class VersionConflict(Exception):
    def __init__(self, current_version: int) -> None:
        super().__init__(f"scratchpad is at version {current_version}")
        self.current_version = current_version


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if not path.exists():
            _write_local(_initial_data())


async def get(version: int | None = None) -> models.ScratchpadVersion | None:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        if version is None:
            row = await (await db.pool()).fetchrow(
                "SELECT v.version, v.content, v.actor, v.created_at "
                "FROM hatchery_scratchpad_versions v "
                "JOIN hatchery_scratchpad_state s ON v.version = s.head_version"
            )
        else:
            row = await (await db.pool()).fetchrow(
                "SELECT version, content, actor, created_at "
                "FROM hatchery_scratchpad_versions WHERE version = $1",
                version,
            )
        return _version_from_row(row) if row is not None else None
    with _lock:
        data = _read_local()
        selected = data["head_version"] if version is None else version
        found = next((item for item in data["versions"] if item["version"] == selected), None)
        return models.ScratchpadVersion.model_validate(found) if found is not None else None


async def state() -> tuple[int, int]:
    """Return (head_version, last_read_version)."""
    _, head_version, last_read_version = await snapshot()
    return head_version, last_read_version


async def snapshot(
    version: int | None = None,
) -> tuple[models.ScratchpadVersion | None, int, int]:
    """Read one immutable version and the global cursors from one snapshot."""
    await ensure_ready()
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(
                "SELECT head_version, last_read_version "
                "FROM hatchery_scratchpad_state WHERE singleton"
            )
            selected = int(row["head_version"]) if version is None else version
            found = await conn.fetchrow(
                "SELECT version, content, actor, created_at "
                "FROM hatchery_scratchpad_versions WHERE version = $1",
                selected,
            )
            return (
                _version_from_row(found) if found is not None else None,
                int(row["head_version"]),
                int(row["last_read_version"]),
            )
    with _lock:
        data = _read_local()
        selected = data["head_version"] if version is None else version
        found = next((item for item in data["versions"] if item["version"] == selected), None)
        return (
            models.ScratchpadVersion.model_validate(found) if found is not None else None,
            int(data["head_version"]),
            int(data["last_read_version"]),
        )


async def list_versions(limit: int = 50) -> list[models.ScratchpadVersionSummary]:
    await ensure_ready()
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT version, actor, created_at FROM hatchery_scratchpad_versions "
            "ORDER BY version DESC LIMIT $1",
            limit,
        )
        return [
            models.ScratchpadVersionSummary(
                version=int(row["version"]),
                actor=models.ScratchpadActor.model_validate(_json(row["actor"])),
                created_at=row["created_at"].isoformat(),
            )
            for row in rows
        ]
    with _lock:
        versions = list(reversed(_read_local()["versions"][-limit:]))
        return [models.ScratchpadVersionSummary.model_validate(item) for item in versions]


async def write(
    content: str,
    expected_version: int,
    actor: models.ScratchpadActor,
    *,
    mark_read: bool = False,
) -> models.ScratchpadVersion:
    """Append a full replacement only when expected_version is still the head."""
    await ensure_ready()
    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError("scratchpad is too large")
    if expected_version < 0:
        raise ValueError("expected_version must not be negative")
    now = datetime.datetime.now(datetime.UTC)
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE hatchery_scratchpad_state "
                "SET head_version = head_version + 1, "
                "last_read_version = CASE WHEN $2 THEN head_version + 1 ELSE last_read_version END "
                "WHERE singleton AND head_version = $1 RETURNING head_version",
                expected_version,
                mark_read,
            )
            if row is None:
                current = await conn.fetchval(
                    "SELECT head_version FROM hatchery_scratchpad_state WHERE singleton"
                )
                raise VersionConflict(int(current))
            version = int(row["head_version"])
            inserted = await conn.fetchrow(
                "INSERT INTO hatchery_scratchpad_versions "
                "(version, content, actor, created_at) VALUES ($1, $2, $3::jsonb, $4) "
                "RETURNING version, content, actor, created_at",
                version,
                content,
                actor.model_dump_json(),
                now,
            )
            return _version_from_row(inserted)
    with _lock:
        data = _read_local()
        if data["head_version"] != expected_version:
            raise VersionConflict(int(data["head_version"]))
        version = expected_version + 1
        saved = models.ScratchpadVersion(
            version=version,
            content=content,
            actor=actor,
            created_at=now.isoformat(),
        )
        data["versions"].append(saved.model_dump(mode="json"))
        data["head_version"] = version
        if mark_read:
            data["last_read_version"] = version
        _write_local(data)
        return saved


async def mark_read(version: int) -> int:
    """Advance the global read cursor to an existing version without regression."""
    await ensure_ready()
    if version < 0:
        raise ValueError("version must not be negative")
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "UPDATE hatchery_scratchpad_state s "
            "SET last_read_version = GREATEST(s.last_read_version, $1) "
            "WHERE s.singleton AND $1 <= s.head_version "
            "AND EXISTS (SELECT 1 FROM hatchery_scratchpad_versions v WHERE v.version = $1) "
            "RETURNING last_read_version",
            version,
        )
        if row is None:
            raise ValueError("unknown scratchpad version")
        return int(row["last_read_version"])
    with _lock:
        data = _read_local()
        if version > data["head_version"] or not any(
            item["version"] == version for item in data["versions"]
        ):
            raise ValueError("unknown scratchpad version")
        data["last_read_version"] = max(data["last_read_version"], version)
        _write_local(data)
        return int(data["last_read_version"])


def structured_diff(before: str, after: str) -> tuple[list[models.ScratchpadDiffLine], bool]:
    """Build a bounded unified line diff for the browser."""
    raw = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="read", tofile="head", lineterm="", n=3
        )
    )
    lines: list[models.ScratchpadDiffLine] = []
    old_line = new_line = 0
    for value in raw[2:]:
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", value)
        if match:
            old_line, new_line = int(match.group(1)), int(match.group(2))
            lines.append(models.ScratchpadDiffLine(kind="header", text=value))
        elif value.startswith("+"):
            lines.append(models.ScratchpadDiffLine(kind="add", text=value[1:], new_line=new_line))
            new_line += 1
        elif value.startswith("-"):
            lines.append(models.ScratchpadDiffLine(kind="remove", text=value[1:], old_line=old_line))
            old_line += 1
        else:
            lines.append(
                models.ScratchpadDiffLine(
                    kind="context", text=value[1:] if value.startswith(" ") else value,
                    old_line=old_line, new_line=new_line,
                )
            )
            old_line += 1
            new_line += 1
        if len(lines) >= MAX_DIFF_LINES:
            return lines, len(raw) - 2 > len(lines)
    return lines, False


def _initial_data() -> dict:
    initial = models.ScratchpadVersion(
        version=0,
        content="",
        actor=models.ScratchpadActor(kind="system"),
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    return {"head_version": 0, "last_read_version": 0, "versions": [initial.model_dump(mode="json")]}


def _version_from_row(row) -> models.ScratchpadVersion:
    return models.ScratchpadVersion(
        version=int(row["version"]),
        content=row["content"],
        actor=models.ScratchpadActor.model_validate(_json(row["actor"])),
        created_at=row["created_at"].isoformat(),
    )


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _path():
    return store.data_dir() / "scratchpad" / "global.json"


def _read_local() -> dict:
    return json.loads(_path().read_text(encoding="utf-8"))


def _write_local(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
