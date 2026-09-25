"""Starter agent directories. `templates/<name>/` is committed to `agents/<id>/` on create."""

import importlib.resources
import importlib.resources.abc

from hatchery import config
from hatchery.workspace import files


def load(name: str = "default") -> dict[str, files.File]:
    """The template as a tree of paths relative to the agent directory."""
    config.validate_slug(name)
    root = importlib.resources.files(__name__)
    template = root.joinpath(name)
    if not template.is_dir():
        available = sorted(
            entry.name for entry in root.iterdir() if entry.is_dir() and entry.name[0] != "_"
        )
        raise FileNotFoundError(f"no agent template named {name!r}; have {available}")
    tree: dict[str, files.File] = {}

    def visit(entry: importlib.resources.abc.Traversable, relative: str) -> None:
        for child in entry.iterdir():
            path = f"{relative}/{child.name}" if relative else child.name
            if child.is_dir():
                if child.name != "__pycache__":
                    visit(child, path)
            else:
                # Wheels do not reliably keep modes: everything under scripts/ is executable.
                executable = path.startswith("scripts/") and child.name != "README.md"
                tree[path] = files.File(child.read_bytes(), executable)

    visit(template, "")
    if "AGENTS.md" not in tree:
        raise ValueError(f"template {name!r} has no AGENTS.md")
    files.validate_tree(tree)
    return tree

