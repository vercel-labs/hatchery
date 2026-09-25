"""Deterministic sandbox double for tests. Command text is matched as data, never executed.

Ported from agentmesh `sandbox/scripted.py`.
"""

import collections.abc
import dataclasses
import pathlib
import re
import typing

from hatchery.worker import provider
from hatchery.workspace import files


@dataclasses.dataclass(frozen=True)
class ScriptedCommand:
    """A declared result plus file effects, so tests can drive Git without a shell."""

    result: provider.ExecResult
    writes: collections.abc.Mapping[str, files.File] = dataclasses.field(default_factory=dict)
    deletes: collections.abc.Sequence[str] = ()


type Script = collections.abc.Mapping[str, provider.ExecResult | ScriptedCommand]


class ScriptedSandbox:
    def __init__(self, name: str, script: Script) -> None:
        self.name, self.script = name, dict(script)
        self.files: dict[str, files.File] = {}
        self.inspection_files: dict[str, bytes] = {}
        self.active = True
        self.commands: list[str] = []
        self.inputs: list[bytes | None] = []
        self.environments: list[dict[str, str]] = []

    def _require_active(self) -> None:
        if not self.active:
            raise RuntimeError("sandbox is stopped; acquire it before use")

    async def exec(
        self,
        command: str,
        *,
        timeout: float,
        env: collections.abc.Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> provider.ExecResult:
        self._require_active()
        if command not in self.script:
            raise AssertionError(f"unscripted command: {command!r}")
        self.commands.append(command)
        self.inputs.append(stdin)
        self.environments.append(dict(env or {}))
        step = self.script[command]
        if isinstance(step, ScriptedCommand):
            for path in (*step.writes, *step.deletes):
                if files.validate_tree_path(path).startswith("collective/"):
                    raise ValueError("scripted commands cannot modify readonly collective")
            updated = {p: f for p, f in self.files.items() if p not in step.deletes}
            updated.update(step.writes)
            files.validate_sandbox_tree(updated)
            self.files, step = updated, step.result
        return step

    async def upload(self, tree: files.Tree) -> None:
        self._require_active()
        updated = {**self.files, **tree}
        files.validate_sandbox_tree(updated)
        self.files = updated

    async def download(self, roots: collections.abc.Sequence[str]) -> dict[str, files.File]:
        self._require_active()
        selected: dict[str, files.File] = {}
        for root in roots:
            files.validate_tree_path(root, allow_root=True)
            selected.update(
                {
                    p: f
                    for p, f in self.files.items()
                    if (p == root or p.startswith(f"{root}/")) and not files.is_python_cache(p)
                }
            )
        return selected

    async def replace(self, roots: collections.abc.Sequence[str], tree: files.Tree) -> None:
        self._require_active()
        for root in roots:
            files.validate_tree_path(root, allow_root=True)
            if root == "collective":
                raise ValueError("scripted commands cannot replace readonly collective")
        retained = {
            path: file
            for path, file in self.files.items()
            if not any(path == root or path.startswith(f"{root}/") for root in roots)
        }
        retained.update(tree)
        files.validate_sandbox_tree(retained)
        self.files = retained

    async def deploy(self, tree: files.Tree, revision: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("revision must be a Git commit SHA")
        await self.replace(["self"], tree)
        self.inspection_files[".hatchery/serve-revision"] = revision.encode()

    async def install_runtime(
        self, runtime_files: collections.abc.Mapping[str, bytes]
    ) -> str:
        if not runtime_files:
            raise ValueError("runtime files must not be empty")
        return f"{provider.WORKSPACE}/.hatchery/runtime/scripted"

    def inspection_tree(self) -> dict[str, bytes]:
        return {
            **{path: file.content for path, file in self.files.items()},
            **self.inspection_files,
        }


class ScriptedSandboxProvider:
    """In-memory sandboxes. `persist=False` loses a sandbox on release, forcing a reseed."""

    def __init__(self, script: Script | None = None, *, persist: bool = True) -> None:
        self.script, self.persist = dict(script or {}), persist
        self.sandboxes: dict[str, ScriptedSandbox] = {}
        self.seeds = 0

    async def acquire(
        self,
        name: str,
        *,
        seed: provider.Seed,
        owner: str = "",
        purpose: provider.SandboxPurpose = "thread",
        chat: provider.ThreadChat | None = None,
    ) -> provider.Acquired:
        existing = self.sandboxes.get(name)
        if existing is not None:
            existing.active = True
            return provider.Acquired(existing, fresh=False)
        sandbox = self.sandboxes[name] = ScriptedSandbox(name, self.script)
        await sandbox.upload(await seed())
        if purpose == "serve":
            sandbox.inspection_files["data/.keep"] = b""
        self.seeds += 1
        return provider.Acquired(sandbox, fresh=True)

    async def release(self, sandbox: provider.Sandbox, *, keep: bool) -> None:
        existing = self.sandboxes.get(sandbox.name)
        if existing is None:
            return
        if existing is not sandbox:
            raise ValueError("sandbox does not belong to this provider")
        existing.active = False
        if not keep or not self.persist:
            del self.sandboxes[sandbox.name]

    async def destroy(self, name: str) -> None:
        existing = self.sandboxes.pop(name, None)
        if existing is not None:
            existing.active = False

    def _existing(self, name: str) -> ScriptedSandbox:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            raise FileNotFoundError("Thread sandbox is unavailable")
        return sandbox

    async def list_directory(self, name: str, path: str = "") -> provider.SandboxDirectory:
        provider.validate_workspace_path(path, allow_root=True)
        tree = self._existing(name).inspection_tree()
        prefix = f"{path}/" if path else ""
        directories = {"self", "wiki", "collective", "scratchpad", "repos", ".hatchery"}
        for candidate in tree:
            directories.update(
                str(parent)
                for parent in pathlib.PurePosixPath(candidate).parents
                if str(parent) != "."
            )
        kinds: dict[str, typing.Literal["file", "directory", "symlink", "other"]] = {}
        for directory in directories:
            if directory.rpartition("/")[0] == path:
                kinds[directory.rpartition("/")[2]] = "directory"
        for candidate in tree:
            provider.validate_workspace_path(candidate)
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            if not remainder:
                continue
            child, separator, _ = remainder.partition("/")
            kinds[child] = "directory" if separator else kinds.get(child, "file")
        if path and path not in directories:
            raise FileNotFoundError("Directory is not present in this sandbox")
        ordered = sorted(kinds.items(), key=lambda item: (item[1] != "directory", item[0]))
        truncated = len(ordered) > provider.MAX_DIRECTORY_ENTRIES
        entries = tuple(
            provider.SandboxEntry(child, f"{path}/{child}" if path else child, kind)
            for child, kind in ordered[: provider.MAX_DIRECTORY_ENTRIES]
        )
        return provider.SandboxDirectory(path, entries, truncated)

    async def read_file(
        self, name: str, path: str, *, limit: int
    ) -> provider.SandboxFilePreview:
        provider.validate_workspace_path(path)
        if limit <= 0:
            raise ValueError("file preview limit must be positive")
        content = self._existing(name).inspection_tree().get(path)
        if content is None:
            raise FileNotFoundError("File is not present in this sandbox")
        return provider.SandboxFilePreview(path, content[:limit], len(content) > limit)
