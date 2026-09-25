"""The runtime layer: skills and scripts that ship with Hatchery itself.

`skills/<name>/SKILL.md` documents platform features (API routes, schedules, coding)
and `scripts/<name>` are the default helpers on every agent's `PATH`. `sandbox_files()`
adds the handler SDK (`hatchery.sdk`) for installation beside them. They describe the
deployed runtime, so they are never copied into an agent directory: a copy would
describe an older version. Every agent sees the current layer on its next turn after a
deploy. Runtime skills cannot be shadowed by team or agent skills.
"""

import functools
import importlib.resources
import importlib.resources.abc
import pathlib
import types

import hatchery
from hatchery.workspace import files

PREFIX = "runtime/"
SKILLS_PREFIX = f"{PREFIX}skills/"
SCRIPTS_PREFIX = f"{PREFIX}scripts/"


@functools.cache
def tree() -> files.Tree:
    """Runtime skills and scripts keyed as `runtime/skills/...` and `runtime/scripts/...`."""
    root = importlib.resources.files(__name__)
    collected: dict[str, files.File] = {}

    def visit(entry: importlib.resources.abc.Traversable, relative: str) -> None:
        for child in entry.iterdir():
            path = f"{relative}/{child.name}" if relative else child.name
            if child.is_dir():
                if child.name != "__pycache__":
                    visit(child, path)
            elif not child.name.endswith((".py", ".pyc", ".pyo")):
                # Wheels do not reliably keep modes: everything under scripts/ is executable.
                collected[PREFIX + path] = files.File(
                    child.read_bytes(), path.startswith("scripts/")
                )

    visit(root, "")
    if not any(path.startswith(SKILLS_PREFIX) for path in collected):
        raise RuntimeError("the runtime layer has no skills")
    files.validate_tree(collected)
    return types.MappingProxyType(collected)


def scripts() -> dict[str, bytes]:
    """Executable helpers keyed by their `scripts/<name>` path, for sandbox installation."""
    return {
        path.removeprefix(PREFIX): file.content
        for path, file in tree().items()
        if path.startswith(SCRIPTS_PREFIX)
    }


def sandbox_files() -> dict[str, bytes]:
    """Everything installed into sandboxes: the stdlib-only `hatchery.sdk` plus the scripts.

    Agentmesh `sandbox/runtime.py` `runtime_files()`. The SDK is copied from this
    worker's own package, so handlers always run the SDK version that serves them.
    """
    package = pathlib.Path(hatchery.__file__).resolve().parent
    return {
        "hatchery/__init__.py": (package / "__init__.py").read_bytes(),
        "hatchery/sdk/__init__.py": (package / "sdk" / "__init__.py").read_bytes(),
        "hatchery/sdk/__main__.py": (package / "sdk" / "__main__.py").read_bytes(),
        **scripts(),
    }
