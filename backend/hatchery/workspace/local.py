"""Seed a local workspace repository and its ignored bare remote for development."""

import asyncio
import contextlib
import fcntl
import os
import pathlib

from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git


async def initialize_local(root: pathlib.Path) -> str:
    """Seed a local bare origin without sweeping arbitrary files into a commit."""

    def work() -> str:
        directory = root.absolute()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("workspace root must be an existing regular directory")
        git_dir = directory / ".git"
        if git_dir.is_symlink() or (git_dir.exists() and not git_dir.is_dir()):
            raise ValueError("linked worktrees and symlinked Git directories are not supported")
        with workspace_git.Git(directory.as_uri()) as git, contextlib.ExitStack() as cleanup:
            fresh = not git_dir.exists()
            if fresh:
                result = workspace_git._execute(
                    ["init", "--initial-branch=main", "--template=", str(directory)], git.env
                )
                if result.returncode:
                    raise workspace_git.GitError(
                        "could not initialize workspace working repository"
                    )
            lock_fd = os.open(
                git_dir / "hatchery-init.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            lock = cleanup.enter_context(os.fdopen(lock_fd, "w"))
            fcntl.flock(lock, fcntl.LOCK_EX)
            origin = git.command(
                "config",
                "--file",
                str(git_dir / "config"),
                "--no-includes",
                "--get",
                "remote.origin.url",
                check=False,
            )
            if origin.returncode not in (0, 1):
                raise workspace_git.GitError("could not read existing origin")
            existing = None
            if origin.returncode == 0:
                existing = origin.stdout.decode().strip()
                workspace_git.parse_remote(existing)
            state = directory / ".hatchery"
            remote = state / "remote.git"
            if existing is None:
                if state.is_symlink() or (state.exists() and not state.is_dir()):
                    raise ValueError(".hatchery must be a regular directory")
                ignore = directory / ".gitignore"
                if ignore.is_symlink() or (ignore.exists() and not ignore.is_file()):
                    raise ValueError(".gitignore must be a regular file")
                ignored = ignore.read_text() if ignore.exists() else ""
                if "/.hatchery/" not in ignored.splitlines():
                    ignore.write_text(
                        ignored
                        + ("\n" if ignored and not ignored.endswith("\n") else "")
                        + "/.hatchery/\n"
                    )
                state.mkdir(exist_ok=True, mode=0o700)
                if remote.is_symlink() or (remote.exists() and not remote.is_dir()):
                    raise ValueError("local remote must be a regular bare repository")
                if not remote.exists():
                    result = workspace_git._execute(
                        ["init", "--bare", "--initial-branch=main", "--template=", str(remote)],
                        git.env,
                    )
                    if result.returncode:
                        raise workspace_git.GitError("could not initialize local bare remote")
            main_result = git.command(
                "rev-parse", "--verify", "refs/heads/main", directory=git_dir, check=False
            )
            if main_result.returncode:
                head = git.command("rev-parse", "--verify", "HEAD", directory=git_dir, check=False)
                if head.returncode == 0:
                    sha = head.stdout.decode().strip()
                else:
                    seed: dict[str, workspace_files.File] = {}
                    candidates = [directory / name for name in ("README.md", ".gitignore")]
                    for name in ("agents", "wiki"):
                        tree = directory / name
                        if tree.is_symlink():
                            raise ValueError("workspace seed cannot contain symlinks")
                        if tree.exists():
                            for parent, dirs, names in os.walk(tree, followlinks=False):
                                if any(
                                    (pathlib.Path(parent) / child).is_symlink() for child in dirs
                                ):
                                    raise ValueError("workspace seed cannot contain symlinks")
                                candidates.extend(pathlib.Path(parent) / child for child in names)
                                if len(candidates) > workspace_files.MAX_FILES:
                                    raise ValueError("workspace seed file count exceeds limit")
                    for path in candidates:
                        if not path.exists() and not path.is_symlink():
                            continue
                        if path.is_symlink() or not path.is_file():
                            raise ValueError("workspace seed must contain only regular files")
                        relative = path.relative_to(directory).as_posix()
                        if any(
                            part == ".env" or part.startswith(".env.")
                            for part in path.relative_to(directory).parts
                        ):
                            raise ValueError(
                                "environment files must not be committed to a shared workspace"
                            )
                        if path.stat().st_size > workspace_files.MAX_FILE_BYTES:
                            raise ValueError("workspace seed file exceeds byte limit")
                        seed[relative] = workspace_files.File(
                            path.read_bytes(), bool(path.stat().st_mode & 0o111)
                        )
                        if (
                            sum(len(value.content) for value in seed.values())
                            > workspace_files.MAX_TREE_BYTES
                        ):
                            raise ValueError("workspace seed tree exceeds byte limit")
                    workspace_files.validate_tree(seed)
                    sha = git.commit(
                        None,
                        seed,
                        prefixes=("README.md", ".gitignore", "agents/", "wiki/"),
                        owner="hatchery",
                        message="Initialize shared hatchery storage\n",
                    )
                    git.command("fetch", "--no-tags", git.path.as_uri(), sha, directory=git_dir)
                git.command("update-ref", "refs/heads/main", sha, "0" * 40, directory=git_dir)
                if fresh:
                    env = {**git.env, "GIT_INDEX_FILE": str(git_dir / "index")}
                    result = workspace_git._execute([f"--git-dir={git_dir}", "read-tree", sha], env)
                    if result.returncode:
                        raise workspace_git.GitError("could not initialize working index")
            else:
                sha = main_result.stdout.decode().strip()
            if existing is not None:
                return existing
            git.run("fetch", "--no-tags", directory.as_uri(), "refs/heads/main:refs/heads/main")
            git.remote = remote.as_uri()
            existing_main = git.fetch("main", optional=True)
            if existing_main != sha and not git.push(sha, "main", None):
                raise workspace_git.GitError(
                    "existing local remote has different history; refusing to overwrite"
                )
            git.command(
                "config",
                "--file",
                str(git_dir / "config"),
                "--no-includes",
                "--add",
                "remote.origin.url",
                remote.as_uri(),
            )
            git.command(
                "config",
                "--file",
                str(git_dir / "config"),
                "--no-includes",
                "--add",
                "remote.origin.fetch",
                "+refs/heads/*:refs/remotes/origin/*",
            )
            return remote.as_uri()

    return await asyncio.to_thread(work)
