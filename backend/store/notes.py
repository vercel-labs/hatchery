"""Shared markdown notes scoped to one space."""

import datetime
import json
import threading
import urllib.parse

import models
import store

MAX_CONTENT_LENGTH = 32_000
MAX_FILENAME_LENGTH = 100
MAX_NOTES_PER_SPACE = 50

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_space_notes (
    space_id   TEXT NOT NULL REFERENCES hatchery_spaces(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    content    TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (space_id, filename)
);
"""

_schema_ready = False
_lock = threading.Lock()


class NoteExists(Exception):
    pass


class NoteLimitReached(Exception):
    pass


class NoteConflict(Exception):
    def __init__(self, current: models.Note) -> None:
        super().__init__(f"note is at revision {current.revision}")
        self.current = current


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "notes").mkdir(parents=True, exist_ok=True)


async def list_for_space(space_id: str) -> list[models.Note]:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT filename, content, revision, updated_at "
            "FROM hatchery_space_notes WHERE space_id = $1 ORDER BY filename",
            space_id,
        )
        return [_from_row(space_id, row) for row in rows]
    directory = _directory(space_id)
    if not directory.exists():
        return []
    with _lock:
        return [
            _read_local(space_id, path.name) for path in sorted(directory.glob("*.md"))
        ]


async def list_summaries(space_id: str) -> list[models.NoteSummary]:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT filename, revision, updated_at FROM hatchery_space_notes "
            "WHERE space_id = $1 ORDER BY filename",
            space_id,
        )
        return [
            models.NoteSummary(
                filename=row["filename"],
                revision=int(row["revision"]),
                updated_at=row["updated_at"].isoformat(),
            )
            for row in rows
        ]
    directory = _directory(space_id)
    if not directory.exists():
        return []
    with _lock:
        return [
            models.NoteSummary.model_validate(_read_metadata(path.name, space_id))
            for path in sorted(directory.glob("*.md"))
        ]


async def get(space_id: str, filename: str) -> models.Note | None:
    filename = valid_filename(filename)
    await ensure_ready()
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT filename, content, revision, updated_at "
            "FROM hatchery_space_notes WHERE space_id = $1 AND filename = $2",
            space_id,
            filename,
        )
        return _from_row(space_id, row) if row is not None else None
    path = _path(space_id, filename)
    if not path.exists():
        return None
    with _lock:
        return _read_local(space_id, filename)


async def create(space_id: str, filename: str, content: str = "") -> models.Note:
    filename = valid_filename(filename)
    _validate_content(content)
    await ensure_ready()
    now = datetime.datetime.now(datetime.UTC)
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.fetchrow(
                "SELECT id FROM hatchery_spaces WHERE id = $1 FOR UPDATE", space_id
            )
            if (
                await connection.fetchval(
                    "SELECT count(*) FROM hatchery_space_notes WHERE space_id = $1",
                    space_id,
                )
                >= MAX_NOTES_PER_SPACE
            ):
                raise NoteLimitReached(space_id)
            row = await connection.fetchrow(
                "INSERT INTO hatchery_space_notes "
                "(space_id, filename, content, revision, created_at, updated_at) "
                "VALUES ($1, $2, $3, 1, $4, $4) ON CONFLICT DO NOTHING "
                "RETURNING filename, content, revision, updated_at",
                space_id,
                filename,
                content,
                now,
            )
            if row is None:
                raise NoteExists(filename)
            return _from_row(space_id, row)
    path = _path(space_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if path.exists():
            raise NoteExists(filename)
        if len(list(path.parent.glob("*.md"))) >= MAX_NOTES_PER_SPACE:
            raise NoteLimitReached(space_id)
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(content)
        except FileExistsError as error:
            raise NoteExists(filename) from error
        _write_metadata(space_id, filename, revision=1, updated_at=now)
    return models.Note(
        space_id=space_id,
        filename=filename,
        content=content,
        revision=1,
        updated_at=now.isoformat(),
    )


async def update(
    space_id: str, filename: str, content: str, expected_revision: int
) -> models.Note | None:
    filename = valid_filename(filename)
    _validate_content(content)
    if expected_revision < 1:
        raise ValueError("expected_revision must be positive")
    await ensure_ready()
    now = datetime.datetime.now(datetime.UTC)
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "UPDATE hatchery_space_notes "
            "SET content = $3, revision = revision + 1, updated_at = $5 "
            "WHERE space_id = $1 AND filename = $2 AND revision = $4 "
            "RETURNING filename, content, revision, updated_at",
            space_id,
            filename,
            content,
            expected_revision,
            now,
        )
        if row is not None:
            return _from_row(space_id, row)
        current = await get(space_id, filename)
        if current is not None:
            raise NoteConflict(current)
        return None
    path = _path(space_id, filename)
    if not path.exists():
        return None
    with _lock:
        current = _read_local(space_id, filename)
        if current.revision != expected_revision:
            raise NoteConflict(current)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        revision = current.revision + 1
        _write_metadata(space_id, filename, revision=revision, updated_at=now)
        return models.Note(
            space_id=space_id,
            filename=filename,
            content=content,
            revision=revision,
            updated_at=now.isoformat(),
        )


async def delete_for_space(space_id: str) -> None:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "DELETE FROM hatchery_space_notes WHERE space_id = $1", space_id
        )
        return
    directory = _directory(space_id)
    if directory.exists():
        with _lock:
            for path in directory.iterdir():
                path.unlink()
            directory.rmdir()


def valid_filename(filename: str) -> str:
    """Accept one simple ASCII markdown filename, never a path."""
    if (
        not filename
        or not filename.isascii()
        or len(filename) > MAX_FILENAME_LENGTH
        or not filename.endswith(".md")
        or filename in {".md", "..md"}
        or filename[0] in {".", "-"}
        or any(
            not (character.isalnum() or character in "._-") for character in filename
        )
        or ".." in filename
    ):
        raise ValueError(
            "filename must be a simple .md name using letters, numbers, dots, dashes, or underscores"
        )
    return filename


def _validate_content(content: str) -> None:
    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError(f"content must be at most {MAX_CONTENT_LENGTH} characters")


def _from_row(space_id: str, row) -> models.Note:
    return models.Note(
        space_id=space_id,
        filename=row["filename"],
        content=row["content"],
        revision=int(row["revision"]),
        updated_at=row["updated_at"].isoformat(),
    )


def _directory(space_id: str):
    return store.data_dir() / "notes" / urllib.parse.quote(space_id, safe="")


def _path(space_id: str, filename: str):
    return _directory(space_id) / valid_filename(filename)


def _metadata_path(space_id: str, filename: str):
    return _directory(space_id) / f".{filename}.meta.json"


def _read_local(space_id: str, filename: str) -> models.Note:
    metadata = _read_metadata(filename, space_id)
    return models.Note(
        space_id=space_id,
        filename=filename,
        content=_path(space_id, filename).read_text(encoding="utf-8"),
        revision=metadata["revision"],
        updated_at=metadata["updated_at"],
    )


def _read_metadata(filename: str, space_id: str) -> dict:
    return json.loads(_metadata_path(space_id, filename).read_text(encoding="utf-8"))


def _write_metadata(
    space_id: str, filename: str, *, revision: int, updated_at: datetime.datetime
) -> None:
    path = _metadata_path(space_id, filename)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "filename": filename,
                "revision": revision,
                "updated_at": updated_at.isoformat(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
