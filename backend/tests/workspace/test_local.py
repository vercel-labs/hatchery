"""Local development storage: a working main and an ignored bare remote."""

import os
import pathlib
import subprocess
import urllib.parse

import pytest

from hatchery.workspace import local as workspace_local


def git(root: pathlib.Path, *args: str, data: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=data,
        capture_output=True,
        check=True,
        env={
            "PATH": os.defpath,
            "HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "operator",
            "GIT_AUTHOR_EMAIL": "operator@example.test",
            "GIT_COMMITTER_NAME": "operator",
            "GIT_COMMITTER_EMAIL": "operator@example.test",
        },
    )
    return result.stdout.decode().strip()


async def test_local_initialization_seeds_only_workspace_and_preserves_existing_origin(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "team"
    root.mkdir()
    (root / "README.md").write_text("Workspace\n")
    (root / ".env").write_text("TOKEN=never-commit\n")
    (root / "unrelated.txt").write_text("Not workspace\n")

    remote = await workspace_local.initialize_local(root)
    bare = pathlib.Path(urllib.parse.unquote(urllib.parse.urlsplit(remote).path))

    assert git(bare, "ls-tree", "-r", "--name-only", "main").splitlines() == [
        ".gitignore",
        "README.md",
    ]
    assert git(root, "check-ignore", ".hatchery/remote.git") == ".hatchery/remote.git"
    assert git(root, "remote", "get-url", "origin") == remote
    assert await workspace_local.initialize_local(root) == remote
    assert git(root, "config", "--get-all", "remote.origin.url") == remote

    external = "https://github.com/acme/existing.git"
    git(root, "remote", "set-url", "origin", external)
    assert await workspace_local.initialize_local(root) == external
    assert git(root, "remote", "get-url", "origin") == external


async def test_initialization_creates_missing_main_without_replacing_or_pushing_existing_origin(
    tmp_path: pathlib.Path,
) -> None:
    git(tmp_path, "init", "--template=", "--initial-branch=topic")
    git(tmp_path, "remote", "add", "origin", "https://github.com/acme/existing.git")
    (tmp_path / "README.md").write_text("Seed\n")

    assert (
        await workspace_local.initialize_local(tmp_path) == "https://github.com/acme/existing.git"
    )
    assert git(tmp_path, "show", "main:README.md") == "Seed"
    assert not (tmp_path / ".hatchery").exists()


async def test_initialization_preserves_an_existing_dirty_index(tmp_path: pathlib.Path) -> None:
    git(tmp_path, "init", "--template=", "--initial-branch=main")
    (tmp_path / "README.md").write_text("Committed\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "Seed")
    (tmp_path / "README.md").write_text("Staged but not published\n")
    git(tmp_path, "add", "README.md")
    before = (tmp_path / ".git/index").read_bytes()

    remote = pathlib.Path(
        urllib.parse.unquote(
            urllib.parse.urlsplit(await workspace_local.initialize_local(tmp_path)).path
        )
    )
    assert git(remote, "show", "main:README.md") == "Committed"
    assert (tmp_path / ".git/index").read_bytes() == before


@pytest.mark.parametrize(
    "kind", ["symlink", "environment"], ids=["linked-workspace", "secret-file"]
)
async def test_initialization_refuses_unsafe_workspace_seed(
    tmp_path: pathlib.Path, kind: str
) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    if kind == "symlink":
        (wiki / "outside").symlink_to("/etc/passwd")
    else:
        (wiki / ".env.local").write_text("TOKEN=secret")
    with pytest.raises(ValueError):
        await workspace_local.initialize_local(tmp_path)
