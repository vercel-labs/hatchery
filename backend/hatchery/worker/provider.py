"""The sandbox contract: an isolated Linux filesystem plus a shell, reacquired by name.

Ported from agentmesh `sandbox/base.py`. Only the deterministic sandbox *name* is
durable (a string in thread state). Live SDK objects are reacquired by name inside an
activation and never checkpointed. The agent sees `/workspace/self` (its agent
directory), `/workspace/wiki` (shared), `/workspace/collective/<agent>` (other agents,
read-only), `/workspace/scratchpad` (writable, not checkpointed), `/workspace/repos`
(coding repositories), and `/workspace/.hatchery` (runtime helpers and venvs).
"""

import collections.abc
import dataclasses
import hashlib
import pathlib
import re
import typing

from hatchery.workspace import files

WORKSPACE = "/workspace"
MAX_DIRECTORY_ENTRIES = 2_000

type Seed = collections.abc.Callable[[], collections.abc.Awaitable[files.Tree]]
type SandboxPurpose = typing.Literal["thread", "serve"]


def sandbox_name(process_id: str) -> str:
    """A provider-portable name, stable across activation redelivery."""
    if not process_id:
        raise ValueError("process_id must not be empty")
    return f"hatchery-{hashlib.sha256(process_id.encode()).hexdigest()[:40]}"


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", name):
        raise ValueError("sandbox name must be 1-63 portable letters, digits, dots, '_' or '-'")
    return name


def validate_workspace_path(path: str, *, allow_root: bool = False) -> str:
    """Validate a path relative to `/workspace`, including non-memory roots."""
    if path == "" and allow_root:
        return path
    parsed = pathlib.PurePosixPath(path)
    if (
        not path
        or parsed.is_absolute()
        or not parsed.parts
        or str(parsed) != path
        or "\\" in path
        or any(part in (".", "..") for part in parsed.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or len(path.encode()) > 4096
    ):
        raise ValueError("workspace path must be canonical and relative to /workspace")
    return path


@dataclasses.dataclass(frozen=True)
class ThreadChat:
    """The Hatchery chat that owns a thread sandbox.

    The thread sandbox is a normal chat sandbox (a worker record with the daemon), so
    terminals, SSH, and fx subagents work in it. `user_id` is the actor whose GitHub
    connection clones `repos` into `/workspace/repos`; without one, cloning is skipped
    or fails quietly and the sandbox still starts.
    """

    id: str
    user_id: str | None = None
    repos: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SandboxEntry:
    name: str
    path: str
    kind: typing.Literal["file", "directory", "symlink", "other"]


@dataclasses.dataclass(frozen=True)
class SandboxDirectory:
    path: str
    entries: tuple[SandboxEntry, ...]
    truncated: bool = False


@dataclasses.dataclass(frozen=True)
class SandboxFilePreview:
    path: str
    content: bytes
    truncated: bool = False


@dataclasses.dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def timed_out(self) -> bool:
        return self.exit_code == 124


class SandboxCommandStopped(RuntimeError):
    """The command result was lost, but stopping the sandbox proved it cannot continue."""


class SandboxCommandUncertain(RuntimeError):
    """The command result was lost and the sandbox could not be stopped conclusively."""


class Sandbox(typing.Protocol):
    @property
    def name(self) -> str: ...

    async def exec(
        self,
        command: str,
        *,
        timeout: float,
        env: collections.abc.Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Run Bash in self with its requirements environment. Timeout yields exit 124."""
        ...

    async def upload(self, tree: files.Tree) -> None:
        """Overlay regular files without deleting unrelated ones."""
        ...

    async def download(self, roots: collections.abc.Sequence[str]) -> dict[str, files.File]:
        """Export regular files; missing paths, links, and `.git` are skipped."""
        ...

    async def replace(self, roots: collections.abc.Sequence[str], tree: files.Tree) -> None:
        """Replace complete writable roots with a validated tree."""
        ...

    async def deploy(self, tree: files.Tree, revision: str) -> None:
        """Atomically install one serve revision without changing persistent data."""
        ...

    async def install_runtime(
        self, runtime_files: collections.abc.Mapping[str, bytes]
    ) -> str:
        """Install trusted runtime files outside the agent's roots and return their directory."""
        ...


@dataclasses.dataclass(frozen=True)
class Acquired:
    sandbox: Sandbox
    fresh: bool


class SandboxProvider(typing.Protocol):
    async def acquire(
        self,
        name: str,
        *,
        seed: Seed,
        owner: str = "",
        purpose: SandboxPurpose = "thread",
        chat: ThreadChat | None = None,
    ) -> Acquired:
        """Get or create by name. Only a fresh filesystem calls `seed` and installs it.

        `owner` is the agent ID. A thread sandbox also names its owning `chat`.
        Callers serialize acquire/release for one name; providers do not.
        """
        ...

    async def release(self, sandbox: Sandbox, *, keep: bool) -> None:
        """Idempotently stop and retain the filesystem, or destroy the sandbox."""
        ...

    async def destroy(self, name: str) -> None:
        """Idempotently destroy one named sandbox without creating it."""
        ...

    async def list_directory(self, name: str, path: str = "") -> SandboxDirectory:
        """List one directory in an existing sandbox without creating or seeding it."""
        ...

    async def read_file(self, name: str, path: str, *, limit: int) -> SandboxFilePreview:
        """Read at most `limit` bytes from a regular file in an existing sandbox."""
        ...
