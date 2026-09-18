"""Git-backed files scoped to one configured agents repository."""

import asyncio
import contextvars
import os
import pathlib
import re
import shutil
import subprocess
import threading
import urllib.parse

import models
import store
from store import settings

_REPOSITORY_ENV = "HATCHERY_AGENTS_REPOSITORY_URL"
_TOKEN_ENV = "HATCHERY_AGENTS_REPOSITORY_TOKEN"
_BRANCH_ENV = "HATCHERY_AGENTS_REPOSITORY_BRANCH"
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REVISION = re.compile(r"[0-9a-f]{40,64}")
_MAX_FILES = 4096
_MAX_FILE_BYTES = 4 * 1024 * 1024
_repository_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_repository_url", default=None
)
_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_repository_token", default=None
)
_lock = threading.RLock()


class RepositoryNotConfigured(Exception):
    pass


class RepositoryError(Exception):
    pass


class Conflict(Exception):
    def __init__(self, current_revision: str | None) -> None:
        super().__init__(f"agent files are at revision {current_revision}")
        self.current_revision = current_revision


async def configuration() -> tuple[str, str | None] | None:
    saved = await settings.get()
    if saved.memory_repository is not None:
        return (
            f"https://github.com/{saved.memory_repository}.git",
            saved.memory_repository_installation_id,
        )
    url = os.environ.get(_REPOSITORY_ENV)
    if url is None:
        return None
    return url, os.environ.get("HATCHERY_AGENTS_REPOSITORY_INSTALLATION_ID")


async def configured() -> bool:
    return await configuration() is not None


async def _run(function, *args):
    selected = await configuration()
    url, installation_id = selected or ("", None)
    token = os.environ.get(_TOKEN_ENV)
    parsed = urllib.parse.urlparse(url)
    if token is None and parsed.scheme == "https" and parsed.netloc == "github.com":
        import connections

        token = await connections.github_app_token(
            parsed.path.strip("/").removesuffix(".git"), installation_id
        )
    url_marker = _repository_url.set(url)
    token_marker = _token.set(token)
    try:
        return await asyncio.to_thread(function, *args)
    finally:
        _token.reset(token_marker)
        _repository_url.reset(url_marker)


async def snapshot(agent_slug: str) -> models.AgentFilesSnapshot:
    return await _run(_snapshot_current, _valid_slug(agent_slug))


async def list_files(agent_slug: str) -> list[str]:
    return (await snapshot(agent_slug)).files


async def read(
    agent_slug: str, path: str, revision: str | None = None
) -> models.AgentFile | None:
    slug = _valid_slug(agent_slug)
    relative = _valid_path(path)
    if revision is not None and not _REVISION.fullmatch(revision):
        raise ValueError("revision must be a full git object id")
    return await _run(_read, slug, relative, revision)


async def write(
    agent_slug: str,
    path: str,
    content: str,
    *,
    operation_id: str,
    expected_revision: str | None,
) -> models.AgentFilesSnapshot:
    slug = _valid_slug(agent_slug)
    relative = _valid_path(path)
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        raise ValueError("agent file exceeds 4 MiB")
    _valid_operation_id(operation_id)
    if expected_revision is not None and not _REVISION.fullmatch(expected_revision):
        raise ValueError("expected_revision must be a full git object id")
    return await _run(
        _write, slug, {relative: content}, operation_id, expected_revision
    )


async def delete(
    agent_slug: str,
    path: str,
    *,
    operation_id: str,
    expected_revision: str | None,
) -> models.AgentFilesSnapshot:
    slug = _valid_slug(agent_slug)
    relative = _valid_path(path)
    _valid_operation_id(operation_id)
    if expected_revision is not None and not _REVISION.fullmatch(expected_revision):
        raise ValueError("expected_revision must be a full git object id")
    return await _run(
        _write, slug, {relative: None}, operation_id, expected_revision
    )


async def delete_agent(
    agent_slug: str,
    *,
    operation_id: str,
    expected_revision: str | None,
) -> models.AgentFilesSnapshot:
    slug = _valid_slug(agent_slug)
    snapshot_value = await snapshot(slug)
    if snapshot_value.revision != expected_revision:
        raise Conflict(snapshot_value.revision)
    return await _run(
        _write,
        slug,
        {path: None for path in snapshot_value.files},
        operation_id,
        expected_revision,
    )


async def tree(
    agent_slug: str, revision: str | None = None
) -> tuple[models.AgentFilesSnapshot, dict[str, str]]:
    snapshot_value = await snapshot(agent_slug)
    selected = revision or snapshot_value.revision
    files: dict[str, str] = {}
    for path in snapshot_value.files:
        found = await read(agent_slug, path, selected)
        if found is not None:
            files[path] = found.content
    return snapshot_value, files


async def scaffold(
    agent_slug: str,
    *,
    operation_id: str,
    expected_revision: str | None,
) -> models.AgentFilesSnapshot:
    slug = _valid_slug(agent_slug)
    _valid_operation_id(operation_id)
    if expected_revision is not None and not _REVISION.fullmatch(expected_revision):
        raise ValueError("expected_revision must be a full git object id")
    templates = pathlib.Path(__file__).resolve().parents[1] / "agent_templates"
    files = {
        str(path.relative_to(templates)): path.read_text(encoding="utf-8")
        for path in sorted(templates.rglob("*.md"))
    }
    return await _run(
        _write, slug, files, operation_id, expected_revision, True
    )


def _snapshot_current(slug: str) -> models.AgentFilesSnapshot:
    with _lock:
        repository = _repository()
        _sync(repository)
        return _snapshot(repository, slug)


def _read(slug: str, relative: str, revision: str | None) -> models.AgentFile | None:
    with _lock:
        repository = _repository()
        _sync(repository)
        selected = revision or _head(repository)
        if selected is None:
            return None
        result = _git(
            repository,
            "show",
            f"{selected}:agents/{slug}/{relative}",
            check=False,
        )
        if result.returncode:
            return None
        return models.AgentFile(
            agent_slug=slug,
            path=relative,
            content=result.stdout,
            revision=selected,
        )


def _write(
    slug: str,
    files: dict[str, str | None],
    operation_id: str,
    expected_revision: str | None,
    missing_only: bool = False,
) -> models.AgentFilesSnapshot:
    with _lock:
        repository = _repository()
        _sync(repository)
        current = _head(repository)
        if _operation_exists(repository, operation_id):
            return _snapshot(repository, slug)
        if current != expected_revision:
            raise Conflict(current)

        root = repository / "agents" / slug
        for relative, content in files.items():
            path = root.joinpath(*relative.split("/"))
            if missing_only and path.exists():
                continue
            _safe_parent(repository, path)
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_text(content, encoding="utf-8")
        _git(repository, "add", "-A", "--", f"agents/{slug}")
        if not _git(repository, "diff", "--cached", "--quiet", check=False).returncode:
            return _snapshot(repository, slug)
        _git(
            repository,
            "commit",
            "-m",
            f"Update agent {slug}\n\nHatchery-Operation-ID: {operation_id}",
        )
        result = _git(
            repository,
            "push",
            "origin",
            f"HEAD:refs/heads/{_branch()}",
            check=False,
        )
        if result.returncode:
            _sync(repository)
            raise Conflict(_head(repository))
        return _snapshot(repository, slug)


def _repository() -> pathlib.Path:
    url = _repository_url.get() or os.environ.get(_REPOSITORY_ENV)
    if not url:
        raise RepositoryNotConfigured(_REPOSITORY_ENV)
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RepositoryError("repository URL must not contain credentials, query, or fragment")
    if parsed.scheme == "file":
        if os.environ.get("FASTAPI_ENV") not in {"development", "test"}:
            raise RepositoryError("file repositories are allowed only in development and tests")
        if not pathlib.Path(urllib.parse.unquote(parsed.path)).is_absolute():
            raise RepositoryError("file repository path must be absolute")
    elif (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or re.fullmatch(r"/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)
        is None
    ):
        raise RepositoryError("repository URL must be an exact https://github.com/owner/repo URL")

    repository = store.data_dir() / "agent-files" / "repository"
    if repository.exists():
        result = _git(repository, "remote", "get-url", "origin", check=False)
        if result.returncode or result.stdout.strip() != url:
            shutil.rmtree(repository)
    if not repository.exists():
        repository.parent.mkdir(parents=True, exist_ok=True)
        result = _git(repository.parent, "clone", "--no-checkout", "--", url, repository.name, check=False)
        if result.returncode:
            raise RepositoryError(result.stderr.strip())
        _git(repository, "config", "user.name", "Hatchery")
        _git(repository, "config", "user.email", "hatchery@localhost")
    return repository


def _sync(repository: pathlib.Path) -> None:
    _git(repository, "fetch", "--prune", "origin")
    remote = f"refs/remotes/origin/{_branch()}"
    if _git(repository, "show-ref", "--verify", "--quiet", remote, check=False).returncode == 0:
        _git(repository, "checkout", "-B", _branch(), remote)
        _git(repository, "reset", "--hard", remote)
    elif _head(repository) is None:
        _git(repository, "checkout", "--orphan", _branch())
    _git(repository, "clean", "-fdx")


def _snapshot(repository: pathlib.Path, slug: str) -> models.AgentFilesSnapshot:
    revision = _head(repository)
    if revision is None:
        files = []
    else:
        prefix = f"agents/{slug}/"
        result = _git(repository, "ls-tree", "-r", "--name-only", revision, "--", prefix)
        files = [line.removeprefix(prefix) for line in result.stdout.splitlines()]
        if len(files) > _MAX_FILES:
            raise RepositoryError("agent file count exceeds 4096")
    return models.AgentFilesSnapshot(agent_slug=slug, revision=revision, files=files)


def _head(repository: pathlib.Path) -> str | None:
    result = _git(repository, "rev-parse", "--verify", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _operation_exists(repository: pathlib.Path, operation_id: str) -> bool:
    if _head(repository) is None:
        return False
    result = _git(repository, "log", "--format=%B%x00")
    trailer = f"Hatchery-Operation-ID: {operation_id}"
    return any(trailer in message.splitlines() for message in result.stdout.split("\x00"))


def _safe_parent(repository: pathlib.Path, path: pathlib.Path) -> None:
    repository = repository.resolve()
    if not path.resolve(strict=False).is_relative_to(repository):
        raise ValueError("path escapes repository")
    current = path.parent
    while current != repository:
        if current.is_symlink():
            raise ValueError("agent file path must not traverse symlinks")
        current = current.parent
    if path.is_symlink():
        raise ValueError("agent file must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)


def _valid_slug(slug: str) -> str:
    try:
        return models.Agent.valid_slug(slug)
    except ValueError as error:
        raise ValueError("invalid agent slug") from error


def _valid_path(path: str) -> str:
    if not path or not path.isascii() or "\\" in path:
        raise ValueError("path must be a relative ASCII path")
    parsed = pathlib.PurePosixPath(path)
    if parsed.is_absolute() or any(
        part in {"", ".", ".."} or part.casefold() == ".git"
        for part in parsed.parts
    ):
        raise ValueError("path must stay within the agent directory")
    if len(path) > 240:
        raise ValueError("path is too long")
    return str(parsed)


def _valid_operation_id(operation_id: str) -> None:
    if not _OPERATION.fullmatch(operation_id):
        raise ValueError("invalid operation_id")


def _branch() -> str:
    branch = os.environ.get(_BRANCH_ENV, "main")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) or ".." in branch:
        raise RepositoryError("invalid repository branch")
    return branch


def _git(
    directory: pathlib.Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "TMPDIR")
        if key in os.environ
    }
    home = store.data_dir() / "agent-files" / "home"
    home.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C.UTF-8",
        }
    )
    token = _token.get() or os.environ.get(_TOKEN_ENV)
    if token:
        askpass = home / "askpass.sh"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "x-access-token";; *) printf "%s\\n" "$HATCHERY_GIT_TOKEN";; esac\n',
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment["GIT_ASKPASS"] = str(askpass)
        environment["HATCHERY_GIT_TOKEN"] = token
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "protocol.https.allow=always",
            "-C",
            str(directory),
            *arguments,
        ],
        text=True,
        capture_output=True,
        env=environment,
        timeout=120,
        check=False,
    )
    if check and result.returncode:
        raise RepositoryError(result.stderr.strip() or result.stdout.strip())
    return result
