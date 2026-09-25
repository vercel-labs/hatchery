"""Files and trees as they cross the worker, Git, and sandbox boundaries.

A `File` is regular content plus an executable bit; symlinks, host paths, and Git
metadata are not representable. Trees are keyed by canonical POSIX paths.
"""

import collections.abc
import dataclasses
import pathlib
import re
import typing

ROOTS = ("self", "wiki", "collective")
"""The Git-backed roots the agent sees under /workspace."""

SANDBOX_ROOTS = (*ROOTS, "scratchpad")
"""Transferable sandbox roots, including non-checkpointed scratch state."""

MAX_FILES = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class File:
    content: bytes
    executable: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


Tree = collections.abc.Mapping[str, File]


def is_python_cache(path: str) -> bool:
    """Whether a canonical tree path is generated Python bytecode/cache state."""
    parts = pathlib.PurePosixPath(path).parts
    return "__pycache__" in parts or pathlib.PurePosixPath(path).suffix.lower() in (".pyc", ".pyo")


def validate_repo_path(path: str) -> str:
    """A canonical, printable, Git-safe relative path (no traversal or `.git` aliases)."""
    parts = pathlib.PurePosixPath(path).parts
    if (
        not path
        or len(path) > 1024
        or str(pathlib.PurePosixPath(path)) != path
        or path.startswith("/")
        or any(ord(c) < 32 or ord(c) > 126 or c in '\\:*?"<>|' for c in path)
        or any(
            part in (".", "..")
            or len(part) > 255
            or part.endswith((".", " "))
            or part.lower() == ".git"
            or re.fullmatch(r"git~[0-9]+", part, re.IGNORECASE)
            for part in parts
        )
    ):
        raise ValueError(f"unsafe workspace path: {path!r}")
    return path


def validate_tree_path(path: str, *, allow_root: bool = False) -> str:
    """A canonical path beneath a transferable root as seen from /workspace."""
    parsed = pathlib.PurePosixPath(path)
    if (
        not path
        or str(parsed) != path
        or parsed.is_absolute()
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or not parsed.parts
        or parsed.parts[0] not in SANDBOX_ROOTS
        or any(part in ("..", ".") or part.casefold() == ".git" for part in parsed.parts)
        or len(path.encode()) > 4096
        or (len(parsed.parts) < 2 and not allow_root)
    ):
        raise ValueError(
            "file path must be canonical and beneath self/, wiki/, collective/, "
            f"or scratchpad/: {path!r}"
        )
    return path


def validate_tree(
    files: collections.abc.Mapping[str, File | None], *, deletions: bool = False
) -> None:
    """Bound a tree and reject file/directory collisions before it touches Git or a sandbox."""
    if len(files) > MAX_FILES:
        raise ValueError("workspace file count exceeds limit")
    total = 0
    for path, value in files.items():
        validate_repo_path(path)
        if value is None and deletions:
            continue
        if (
            not isinstance(value, File)
            or not isinstance(value.content, bytes)
            or type(value.executable) is not bool
        ):
            raise ValueError("workspace transfers must contain regular File values")
        if len(value.content) > MAX_FILE_BYTES:
            raise ValueError("workspace file exceeds byte limit")
        total += len(value.content)
    if total > MAX_TREE_BYTES:
        raise ValueError("workspace tree exceeds byte limit")
    present = {path for path, value in files.items() if value is not None}
    for path in present:
        if any(str(parent) in present for parent in pathlib.PurePosixPath(path).parents):
            raise ValueError("workspace tree contains a file/directory collision")


def validate_sandbox_tree(tree: Tree) -> None:
    """A tree as seen from /workspace: every path beneath a root, and within limits."""
    for path in tree:
        validate_tree_path(path)
    validate_tree(tree)


def text_tree(
    tree: Tree, prefix: str | tuple[str, ...] = ("self/", "wiki/")
) -> dict[str, dict[str, typing.Any]]:
    """Files as text for a model; binary content is replaced by a marker."""
    encoded: dict[str, dict[str, typing.Any]] = {}
    for path, file in sorted(tree.items()):
        if not path.startswith(prefix):
            continue
        try:
            content = file.content.decode("utf-8")
        except UnicodeDecodeError:
            content = f"<binary, {len(file.content)} bytes>"
        encoded[path] = {"content": content, "executable": file.executable}
    return encoded
