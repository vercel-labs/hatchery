"""Trees cross the sandbox boundary as tar archives; validation happens on the worker.

Ported from agentmesh `sandbox/transfer.py`.
"""

import collections.abc
import io
import tarfile

from hatchery.workspace import files


def pack(tree: files.Tree) -> bytes:
    """A deterministic tar of regular files, paths relative to /workspace."""
    files.validate_sandbox_tree(tree)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(tree):
            file = tree[path]
            info = tarfile.TarInfo(path)
            info.size = len(file.content)
            info.mode = 0o755 if file.executable else 0o644
            archive.addfile(info, io.BytesIO(file.content))
    return buffer.getvalue()


def unpack(data: bytes, roots: collections.abc.Sequence[str]) -> dict[str, files.File]:
    """Regular files beneath the requested roots. Links, devices, and `.git` are skipped."""
    if not data:
        return {}
    if len(data) > files.MAX_TREE_BYTES * 2:
        raise ValueError("sandbox export exceeds byte limit")
    for root in roots:
        files.validate_tree_path(root, allow_root=True)
    tree: dict[str, files.File] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
        for member in archive:
            path = member.name.removeprefix("./")
            if not member.isreg():
                continue
            if any(part == ".git" for part in path.split("/")) or files.is_python_cache(path):
                continue
            if not any(path == root or path.startswith(f"{root}/") for root in roots):
                continue
            files.validate_tree_path(path)
            if member.size > files.MAX_FILE_BYTES:
                raise ValueError(f"sandbox file exceeds byte limit: {path}")
            total += member.size
            if total > files.MAX_TREE_BYTES or len(tree) >= files.MAX_FILES:
                raise ValueError("sandbox export exceeds tree limits")
            source = archive.extractfile(member)
            assert source is not None
            tree[path] = files.File(source.read(), bool(member.mode & 0o111))
    files.validate_sandbox_tree(tree)
    return tree
