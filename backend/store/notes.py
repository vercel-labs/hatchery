"""Shared markdown notes scoped to one space."""

import datetime
import difflib
import json
import threading
import urllib.parse

import models
import store

# Largest integer represented exactly by JSON consumers using IEEE-754 doubles.
MAX_CONTENT_LENGTH = 9_007_199_254_740_991
MAX_FILENAME_LENGTH = 100
MAX_NOTES_PER_SPACE = 50
FUZZY_MATCH_THRESHOLD = 0.92
FUZZY_MATCH_MARGIN = 0.05
FUZZY_MIN_FIND_LENGTH = 16

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
_files_lock = threading.RLock()
_note_locks: dict[tuple[str, str], threading.RLock] = {}


class NoteExists(Exception):
    pass


class NoteLimitReached(Exception):
    pass


class NoteConflict(Exception):
    def __init__(self, current: models.Note) -> None:
        super().__init__(f"note is at revision {current.revision}")
        self.current = current


class FindReplaceError(Exception):
    def __init__(self, reason: str, *, score: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.score = score


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
    found = []
    for path in sorted(directory.glob("*.md")):
        with _note_lock(space_id, path.name):
            if path.exists():
                found.append(_read_local(space_id, path.name))
    return found


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
    found = []
    for path in sorted(directory.glob("*.md")):
        with _note_lock(space_id, path.name):
            if path.exists():
                found.append(
                    models.NoteSummary.model_validate(
                        _read_metadata(path.name, space_id)
                    )
                )
    return found


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
    with _note_lock(space_id, filename):
        if not _path(space_id, filename).exists():
            return None
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
    with _files_lock, _note_lock(space_id, filename):
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
    space_id: str,
    filename: str,
    content: str,
    expected_revision: int,
) -> models.Note | None:
    """Replace one note under lock only if the human's reviewed revision is current."""
    filename = valid_filename(filename)
    _validate_content(content)
    if expected_revision < 1:
        raise ValueError("expected_revision must be positive")
    await ensure_ready()
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT filename, content, revision, updated_at "
                "FROM hatchery_space_notes WHERE space_id = $1 AND filename = $2 "
                "FOR UPDATE",
                space_id,
                filename,
            )
            if row is None:
                return None
            current = _from_row(space_id, row)
            if current.revision != expected_revision:
                raise NoteConflict(current)
            now = datetime.datetime.now(datetime.UTC)
            return _from_row(
                space_id,
                await connection.fetchrow(
                    "UPDATE hatchery_space_notes "
                    "SET content = $3, revision = revision + 1, updated_at = $4 "
                    "WHERE space_id = $1 AND filename = $2 "
                    "RETURNING filename, content, revision, updated_at",
                    space_id,
                    filename,
                    content,
                    now,
                ),
            )
    with _note_lock(space_id, filename):
        if not _path(space_id, filename).exists():
            return None
        current = _read_local(space_id, filename)
        if current.revision != expected_revision:
            raise NoteConflict(current)
        now = datetime.datetime.now(datetime.UTC)
        return _write_local(space_id, filename, content, current.revision + 1, now)


async def find_replace(
    space_id: str, filename: str, find: str, replacement: str
) -> tuple[models.Note, str, float] | None:
    """Find and replace once while holding the note lock for read, match, and write."""
    filename = valid_filename(filename)
    if not find:
        raise ValueError("find must not be empty")
    await ensure_ready()
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT filename, content, revision, updated_at "
                "FROM hatchery_space_notes WHERE space_id = $1 AND filename = $2 "
                "FOR UPDATE",
                space_id,
                filename,
            )
            if row is None:
                return None
            content, match, score = _find_replace(row["content"], find, replacement)
            _validate_content(content)
            now = datetime.datetime.now(datetime.UTC)
            saved = await connection.fetchrow(
                "UPDATE hatchery_space_notes "
                "SET content = $3, revision = revision + 1, updated_at = $4 "
                "WHERE space_id = $1 AND filename = $2 "
                "RETURNING filename, content, revision, updated_at",
                space_id,
                filename,
                content,
                now,
            )
            return _from_row(space_id, saved), match, score
    with _note_lock(space_id, filename):
        if not _path(space_id, filename).exists():
            return None
        current = _read_local(space_id, filename)
        content, match, score = _find_replace(current.content, find, replacement)
        _validate_content(content)
        now = datetime.datetime.now(datetime.UTC)
        saved = _write_local(
            space_id, filename, content, current.revision + 1, now
        )
        return saved, match, score


async def delete(space_id: str, filename: str) -> bool:
    filename = valid_filename(filename)
    await ensure_ready()
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT filename FROM hatchery_space_notes "
                "WHERE space_id = $1 AND filename = $2 FOR UPDATE",
                space_id,
                filename,
            )
            if row is None:
                return False
            await connection.execute(
                "DELETE FROM hatchery_space_notes WHERE space_id = $1 AND filename = $2",
                space_id,
                filename,
            )
            return True
    with _note_lock(space_id, filename):
        path = _path(space_id, filename)
        if not path.exists():
            return False
        path.unlink()
        metadata = _metadata_path(space_id, filename)
        if metadata.exists():
            metadata.unlink()
        return True


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
        with _files_lock:
            for path in directory.glob("*.md"):
                with _note_lock(space_id, path.name):
                    path.unlink(missing_ok=True)
                    _metadata_path(space_id, path.name).unlink(missing_ok=True)
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


def _find_replace(content: str, find: str, replacement: str) -> tuple[str, str, float]:
    exact_index = content.find(find)
    if exact_index >= 0:
        if content.find(find, exact_index + 1) >= 0:
            raise FindReplaceError("ambiguous")
        return (
            content[:exact_index] + replacement + content[exact_index + len(find) :],
            "exact",
            1.0,
        )
    if len(find) < FUZZY_MIN_FIND_LENGTH:
        raise FindReplaceError("low_confidence", score=0.0)

    lines = content.splitlines(keepends=True)
    find_lines = find.splitlines(keepends=True)
    if not lines or len(find_lines) > len(lines):
        raise FindReplaceError("missing")
    candidates = [
        (
            difflib.SequenceMatcher(
                None,
                find,
                "".join(lines[index : index + len(find_lines)]),
                autojunk=False,
            ).ratio(),
            index,
        )
        for index in range(len(lines) - len(find_lines) + 1)
    ]
    candidates.sort(reverse=True)
    best_score, best_index = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0
    if best_score < FUZZY_MATCH_THRESHOLD:
        raise FindReplaceError("low_confidence", score=best_score)
    if second_score >= FUZZY_MATCH_THRESHOLD or best_score - second_score < FUZZY_MATCH_MARGIN:
        raise FindReplaceError("ambiguous", score=best_score)
    lines[best_index : best_index + len(find_lines)] = [replacement]
    return "".join(lines), "fuzzy", best_score


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


def _note_lock(space_id: str, filename: str) -> threading.RLock:
    with _files_lock:
        return _note_locks.setdefault((space_id, filename), threading.RLock())


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


def _write_local(
    space_id: str,
    filename: str,
    content: str,
    revision: int,
    updated_at: datetime.datetime,
) -> models.Note:
    path = _path(space_id, filename)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    _write_metadata(space_id, filename, revision=revision, updated_at=updated_at)
    return models.Note(
        space_id=space_id,
        filename=filename,
        content=content,
        revision=revision,
        updated_at=updated_at.isoformat(),
    )


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
