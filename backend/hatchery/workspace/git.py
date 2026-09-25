"""Isolated Git plumbing: no worktree, inherited config, hooks, or ambient credentials."""

import base64
import collections.abc
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import types
import urllib.parse

from hatchery.workspace import files as workspace_files
from hatchery.workspace import git_runtime

MAX_ATTEMPTS = 4
GIT_TIMEOUT = 120
MAX_EXTRA_PARENTS = 64


class GitError(RuntimeError):
    """A Git operation failed; messages deliberately omit credential-bearing output."""


class MainChanged(GitError):
    """Consolidation must be recomputed against a new main revision."""


class MergeConflict(GitError):
    """A proposal overlaps a change already made on main."""


def _git_runtime() -> git_runtime.GitRuntime:
    try:
        return git_runtime.resolve_git()
    except git_runtime.GitRuntimeUnavailable as exc:
        raise GitError(str(exc)) from exc


def validate_owner(owner: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", owner):
        raise ValueError("owner must be a lowercase workspace slug (1-64 characters)")
    return owner


def validate_text(value: str, name: str, maximum: int = 1024) -> str:
    if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be nonempty single-line text (at most {maximum} characters)")
    return value


def parse_remote(remote: str) -> tuple[str, str]:
    url = urllib.parse.urlsplit(remote)
    if url.query or url.fragment or url.username or url.password or url.port:
        raise ValueError("remote must not embed credentials, ports, query strings, or fragments")
    if url.scheme == "file" and not url.netloc and urllib.parse.unquote(url.path).startswith("/"):
        if any(ord(c) < 32 for c in urllib.parse.unquote(url.path)):
            raise ValueError("invalid file remote")
        return "file", urllib.parse.unquote(url.path)
    if url.scheme == "https" and url.netloc == "github.com":
        match = re.fullmatch(r"/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", url.path)
        if match and match[2] not in (".", ".."):
            return "github", f"{match[1]}/{match[2]}"
    raise ValueError(
        "workspace remote must be an absolute file:// URL or https://github.com/org/repo"
    )


def operation_digest(owner: str, process_id: str, operation_id: str, kind: str) -> str:
    validate_text(process_id, "process_id")
    validate_text(operation_id, "operation_id")
    return hashlib.sha256(json.dumps([owner, process_id, operation_id, kind]).encode()).hexdigest()


def _execute(
    args: collections.abc.Sequence[str],
    env: collections.abc.Mapping[str, str],
    *,
    data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    runtime = _git_runtime()
    try:
        return subprocess.run(
            [runtime.executable, *args],
            input=data,
            capture_output=True,
            env=runtime.environment(env),
            cwd=env["HOME"],
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError("Git failed or exceeded its worker timeout") from exc


@dataclasses.dataclass(frozen=True)
class Entry:
    mode: str
    oid: str
    size: int


@dataclasses.dataclass(frozen=True)
class Merge:
    """A three-way merge result: the merged tree and the paths Git could not merge."""

    tree: str
    conflicts: tuple[str, ...]


MERGE_LABELS = {"refs/consolidate/main": "main", "refs/consolidate/thread": "thread"}
"""Temporary refs name the merge sides so conflict markers read `main` and `thread`."""


def relabel_markers(content: bytes) -> bytes:
    """Rewrite conflict-marker labels from temporary ref names to `main` and `thread`."""
    for ref, label in MERGE_LABELS.items():
        content = content.replace(f"<<<<<<< {ref}".encode(), f"<<<<<<< {label}".encode())
        content = content.replace(f">>>>>>> {ref}".encode(), f">>>>>>> {label}".encode())
    return content


def has_markers(content: bytes) -> bool:
    """Whether a file still carries the `main`/`thread` conflict markers a refresh wrote."""
    lines = content.split(b"\n")
    return b"<<<<<<< main" in lines and b">>>>>>> thread" in lines


class Git:
    """A fresh object database and private index, with no inherited Git configuration."""

    def __init__(self, remote: str, token: str | None = None):
        kind, _ = parse_remote(remote)
        self.kind = kind
        self.remote = remote
        self.checked_remote: str | None = None
        self._entries: dict[str, dict[str, Entry]] = {}
        self._closed = False
        self.temporary = tempfile.TemporaryDirectory(prefix="hatchery-git-")
        root = pathlib.Path(self.temporary.name)
        self.path = root / "objects.git"
        self._config = {
            "core.hooksPath": os.devnull,
            "core.attributesFile": os.devnull,
            "core.fsmonitor": "false",
            "core.untrackedCache": "false",
            "credential.helper": "",
            "http.followRedirects": "false",
            "protocol.allow": "never",
            "protocol.file.allow": "always" if kind == "file" else "never",
            "protocol.https.allow": "always" if kind == "github" else "never",
            "fetch.recurseSubmodules": "false",
            "submodule.recurse": "false",
            "commit.gpgSign": "false",
            "gc.auto": "0",
            "maintenance.auto": "false",
            "receive.autoGC": "false",
        }
        self.env = {
            "PATH": os.defpath,
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_INDEX_FILE": str(root / "index"),
        }
        self.set_token(token)
        result = _execute(["init", "--bare", "--template=", str(self.path)], self.env)
        if result.returncode:
            self.temporary.cleanup()
            raise GitError("could not initialize isolated Git object database")

    def __enter__(self) -> Git:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self.temporary.cleanup()
            self._closed = True

    def object_bytes(self) -> int:
        """Approximate on-disk size of the isolated object database, without running Git."""
        total = 0
        stack = [self.path / "objects"]
        while stack:
            try:
                with os.scandir(stack.pop()) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(pathlib.Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                continue
        return total

    def set_token(self, token: str | None) -> None:
        """Refresh command configuration without retaining an obsolete credential."""
        if token is not None:
            validate_text(token, "token", 16384)
        count = int(self.env.get("GIT_CONFIG_COUNT", "0"))
        for index in range(count):
            self.env.pop(f"GIT_CONFIG_KEY_{index}", None)
            self.env.pop(f"GIT_CONFIG_VALUE_{index}", None)
        config = dict(self._config)
        if token and self.kind == "github":
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            config["http.https://github.com/.extraHeader"] = f"Authorization: Basic {encoded}"
        self.env["GIT_CONFIG_COUNT"] = str(len(config))
        for index, (key, value) in enumerate(config.items()):
            self.env[f"GIT_CONFIG_KEY_{index}"] = key
            self.env[f"GIT_CONFIG_VALUE_{index}"] = value

    def command(
        self,
        *args: str,
        data: bytes | None = None,
        check: bool = True,
        directory: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        result = _execute([f"--git-dir={directory or self.path}", *args], self.env, data=data)
        if check and result.returncode:
            raise GitError(f"Git {args[0]} failed (exit {result.returncode})")
        return result

    def run(self, *args: str, data: bytes | None = None) -> bytes:
        return self.command(*args, data=data).stdout

    def pack(self, service: str) -> list[str]:
        kind, location = parse_remote(self.remote)
        if kind != "file":
            return []
        if self.checked_remote != self.remote:
            bare = self.command(
                "config",
                "--file",
                str(pathlib.Path(location) / "config"),
                "--no-includes",
                "--get",
                "core.bare",
                check=False,
            )
            if bare.returncode or bare.stdout.strip() != b"true":
                raise ValueError("file:// workspace remote must be a bare Git repository")
            self.checked_remote = self.remote
        executable = _git_runtime().executable
        # File transport clears GIT_CONFIG_COUNT in the child. These are fixed worker
        # commands, never model strings; command-line config overrides remote hooks.
        command = (
            f"{shlex.quote(executable)} -c core.hooksPath={shlex.quote(os.devnull)} "
            f"-c core.fsmonitor=false -c receive.autoGC=false {service}-pack"
        )
        return [f"--{service}-pack={command}"]

    def fetch(self, branch: str, *, optional: bool = False) -> str | None:
        return self.fetch_many((branch,), optional=(branch,) if optional else ())[branch]

    def fetch_many(
        self,
        branches: collections.abc.Sequence[str],
        *,
        optional: collections.abc.Sequence[str] = (),
    ) -> dict[str, str | None]:
        """Fetch several branch tips with one remote discovery and one transfer."""
        if len(set(branches)) != len(branches):
            raise ValueError("branches must be unique")
        optional_set = set(optional)
        if not optional_set.issubset(branches):
            raise ValueError("optional branches must be fetched")
        refs = {branch: f"refs/heads/{branch}" for branch in branches}
        if not refs:
            return {}
        output = self.run(
            "ls-remote", "--refs", *self.pack("upload"), "--", self.remote, *refs.values()
        )
        advertised: dict[str, str] = {}
        expected_refs = {ref: branch for branch, ref in refs.items()}
        for line in output.splitlines():
            try:
                raw_sha, raw_ref = line.split(b"\t", 1)
                sha, ref = raw_sha.decode(), raw_ref.decode()
            except (UnicodeDecodeError, ValueError) as exc:
                raise GitError("Git returned an invalid remote ref listing") from exc
            branch = expected_refs.get(ref)
            if branch is None or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise GitError("Git returned an invalid remote ref listing")
            advertised[branch] = sha
        missing = [branch for branch in branches if branch not in advertised]
        required_missing = [branch for branch in missing if branch not in optional_set]
        if required_missing:
            raise GitError(f"missing workspace branch: {required_missing[0]}")
        fetched: dict[str, str | None] = dict.fromkeys(branches)
        if advertised:
            self.run(
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                *self.pack("upload"),
                self.remote,
                *(f"+{refs[branch]}:{refs[branch]}" for branch in advertised),
            )
            # The remote may move between discovery and transfer; the fetched local
            # ref is authoritative, exactly as a single fetch-then-rev-parse was.
            checks = self.run(
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
                data="".join(f"{refs[branch]}\n" for branch in advertised).encode(),
            ).splitlines()
            if len(checks) != len(advertised):
                raise GitError("Git did not resolve every fetched branch")
            for branch, check in zip(advertised, checks, strict=True):
                fields = check.decode(errors="replace").split()
                if len(fields) != 2 or fields[1] != "commit":
                    raise GitError("workspace branch does not point to a commit")
                if not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
                    raise GitError("Git returned an invalid commit")
                fetched[branch] = fields[0]
        return fetched

    def push(self, sha: str, branch: str, expected: str | None) -> bool:
        ref = f"refs/heads/{branch}"
        return (
            self.command(
                "push",
                "--porcelain",
                *self.pack("receive"),
                f"--force-with-lease={ref}:{expected or ''}",
                self.remote,
                f"{sha}:{ref}",
                check=False,
            ).returncode
            == 0
        )

    def delete(self, branch: str, expected: str) -> bool:
        """Delete a remote branch only while it still points at `expected`."""
        ref = f"refs/heads/{branch}"
        return (
            self.command(
                "push",
                "--porcelain",
                *self.pack("receive"),
                f"--force-with-lease={ref}:{expected}",
                self.remote,
                f":{ref}",
                check=False,
            ).returncode
            == 0
        )

    def merge_base(self, left: str, right: str) -> str:
        sha = self.run("merge-base", left, right).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitError("Git returned an invalid merge base")
        return sha

    def forks_at(self, revision: str, upstream: str, fork: str) -> bool:
        """Whether `revision` left upstream's first-parent history exactly at `fork`."""
        if revision == fork:
            return True
        raw_count = self.run("rev-list", "--first-parent", "--count", f"{fork}..{revision}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise GitError("Git returned an invalid first-parent count") from exc
        if count < 1:
            return False
        first = self.resolve_commit(f"{revision}~{count - 1}")
        parents = self.commit_parents(first)
        return bool(parents and parents[0] == fork and not self.is_ancestor(first, upstream))

    def resolve_commit(self, revision: str) -> str:
        sha = self.run("rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitError("Git returned an invalid commit")
        return sha

    def entries(self, revision: str) -> dict[str, Entry]:
        cached = self._entries.get(revision)
        if cached is not None:
            return dict(cached)
        result = {}
        for record in self.run("ls-tree", "-r", "-l", "-z", "--full-tree", revision).split(b"\0"):
            if not record:
                continue
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, oid, size = meta.split()
            path = raw_path.decode("utf-8", errors="strict")
            result[path] = Entry(mode.decode(), oid.decode(), int(size) if kind == b"blob" else -1)
        if re.fullmatch(r"[0-9a-f]{40}", revision):
            self._entries[revision] = result
        return dict(result)

    @staticmethod
    def _validate_entries(entries: collections.abc.Mapping[str, Entry]) -> None:
        if (
            len(entries) > workspace_files.MAX_FILES
            or sum(entry.size for entry in entries.values()) > workspace_files.MAX_TREE_BYTES
        ):
            raise ValueError("workspace tree exceeds transfer limits")
        shape = {}
        for path, entry in entries.items():
            workspace_files.validate_repo_path(path)
            if (
                entry.mode not in ("100644", "100755")
                or not 0 <= entry.size <= workspace_files.MAX_FILE_BYTES
            ):
                raise ValueError("workspace contains a link, special file, or oversized blob")
            shape[path] = workspace_files.File(b"", entry.mode == "100755")
        workspace_files.validate_tree(shape)

    def read(self, entries: collections.abc.Mapping[str, Entry]) -> dict[str, workspace_files.File]:
        self._validate_entries(entries)
        ordered = list(entries.items())
        if not ordered:
            return {}
        output = self.run(
            "cat-file", "--batch", data="".join(f"{entry.oid}\n" for _, entry in ordered).encode()
        )
        files = {}
        offset = 0
        for path, entry in ordered:
            end = output.find(b"\n", offset)
            if end < 0:
                raise GitError("Git returned a truncated blob batch")
            header = output[offset:end].split()
            if header != [entry.oid.encode(), b"blob", str(entry.size).encode()]:
                raise GitError("Git blob did not match its tree entry")
            start = end + 1
            finish = start + entry.size
            if finish >= len(output) or output[finish : finish + 1] != b"\n":
                raise GitError("Git returned a truncated blob batch")
            files[path] = workspace_files.File(output[start:finish], entry.mode == "100755")
            offset = finish + 1
        if offset != len(output):
            raise GitError("Git returned unexpected blob batch data")
        workspace_files.validate_tree(files)
        return files

    def blob(self, oid: str) -> bytes:
        """Read one blob after verifying its object id and type."""
        if not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise GitError("invalid Git blob id")
        return self.run("cat-file", "blob", oid)

    def diff(self, path: str, before: Entry | None, after: Entry | None) -> bytes:
        """Render one file diff without a worktree, attributes, or external drivers."""
        workspace_files.validate_repo_path(path)
        if before is None and after is None:
            return b""
        with tempfile.TemporaryDirectory(prefix="diff-", dir=self.temporary.name) as directory:
            root = pathlib.Path(directory)

            def materialize(side: str, entry: Entry | None) -> str:
                if entry is None:
                    return os.devnull
                target = root / side / path
                target.parent.mkdir(parents=True)
                target.write_bytes(self.blob(entry.oid))
                target.chmod(0o755 if entry.mode == "100755" else 0o644)
                return str(target)

            result = self.command(
                "diff",
                "--no-index",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                materialize("before", before),
                materialize("after", after),
                check=False,
            )
        if result.returncode not in (0, 1):
            raise GitError("Git file diff failed")
        return result.stdout

    def commit_message(self, revision: str) -> bytes:
        """Read a commit's complete message."""
        return self.run("show", "-s", "--format=%B", f"{revision}^{{commit}}")

    def commit_parents(self, revision: str) -> tuple[str, ...]:
        """Read and validate a commit's ordered parent ids."""
        parents = tuple(
            self.run("show", "-s", "--format=%P", f"{revision}^{{commit}}").decode().split()
        )
        if any(not re.fullmatch(r"[0-9a-f]{40}", parent) for parent in parents):
            raise GitError("Git returned an invalid commit parent")
        return parents

    def hash_objects(
        self, contents: collections.abc.Sequence[bytes], *, write: bool = False
    ) -> list[str]:
        """Hash in-memory blobs in one Git process, optionally storing the objects."""
        if not contents:
            return []
        with tempfile.TemporaryDirectory(prefix="blobs-", dir=self.temporary.name) as directory:
            paths = []
            for index, content in enumerate(contents):
                path = pathlib.Path(directory) / str(index)
                path.write_bytes(content)
                paths.append(str(path))
            # --no-filters: bytes must round-trip exactly; never apply eol or clean filters.
            args = ["hash-object", "--no-filters"]
            if write:
                args.append("-w")
            args.append("--stdin-paths")
            output = self.run(*args, data="".join(f"{path}\n" for path in paths).encode())
        oids = output.decode().splitlines()
        if len(oids) != len(contents) or any(
            not re.fullmatch(r"[0-9a-f]{40}", oid) for oid in oids
        ):
            raise GitError("Git did not hash every blob")
        return oids

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return (
            self.command(
                "merge-base", "--is-ancestor", ancestor, descendant, check=False
            ).returncode
            == 0
        )

    def subtree(self, revision: str, prefix: str) -> str | None:
        """The tree id under `prefix` at a commit, or None when the directory is absent."""
        result = self.command(
            "rev-parse", "--verify", "-q", f"{revision}:{prefix.rstrip('/')}", check=False
        )
        oid = result.stdout.decode().strip()
        return oid if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", oid) else None

    def project(self, revisions: collections.abc.Sequence[str], prefix: str) -> list[str]:
        """Root trees holding only each revision's regular entries below `prefix`.

        The section subtree is reused as-is and re-wrapped in its directory chain, so
        no blob is read; entries are validated from the tree listing alone. A revision
        without the directory projects to the empty tree.
        """
        directory = prefix.rstrip("/")
        workspace_files.validate_repo_path(directory)
        for revision in revisions:
            self._validate_entries(
                {p: e for p, e in self.entries(revision).items() if p.startswith(prefix)}
            )
        checks = self.run(
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            data="".join(f"{revision}:{directory}\n" for revision in revisions).encode(),
        ).splitlines()
        if len(checks) != len(revisions):
            raise GitError("Git did not resolve every section directory")
        current: list[str | None] = []
        for check in checks:
            fields = check.decode(errors="replace").split()
            if fields[-1:] == ["missing"]:
                current.append(None)
            elif (
                len(fields) == 2
                and fields[1] == "tree"
                and re.fullmatch(r"[0-9a-f]{40}", fields[0])
            ):
                current.append(fields[0])
            else:
                raise GitError("workspace section path is not a directory")
        # Wrap inner-first: agents/alice -> {alice: T} -> {agents: T'}.
        for name in reversed(directory.split("/")):
            wanted = [oid for oid in current if oid is not None]
            if wanted:
                # Each block is LF-terminated; one extra LF separates trees.
                blocks = "\n".join(f"040000 tree {oid}\t{name}\n" for oid in wanted)
                produced = self.run("mktree", "--batch", data=blocks.encode()).decode().split()
                if len(produced) != len(wanted) or any(
                    not re.fullmatch(r"[0-9a-f]{40}", oid) for oid in produced
                ):
                    raise GitError("Git did not build every projected tree")
                replacements = iter(produced)
                current = [None if oid is None else next(replacements) for oid in current]
        if any(oid is None for oid in current):
            empty = self.run("mktree", data=b"").decode().strip()
            if not re.fullmatch(r"[0-9a-f]{40}", empty):
                raise GitError("Git did not create the empty tree")
            current = [empty if oid is None else oid for oid in current]
        return [oid for oid in current if oid is not None]

    def merged_thread_head(self, main: str, process_id: str, section: str) -> str | None:
        """The thread commit of this thread's newest `section` proposal reachable from main."""
        output = self.run(
            "log",
            "--fixed-strings",
            f"--grep=Rotor-Process: {process_id}",
            "--format=%(trailers:key=Rotor-Process,valueonly)%x00"
            "%(trailers:key=Rotor-Section,valueonly)%x00"
            "%(trailers:key=Rotor-Thread,valueonly)%x00%x00",
            main,
            "--",
        )
        for record in output.split(b"\0\0"):
            fields = [field.strip() for field in record.decode(errors="replace").split("\0")]
            if fields[:2] == [process_id, section] and re.fullmatch(r"[0-9a-f]{40}", fields[2]):
                return fields[2]
        return None

    def checkpoint_paths(self, base: str, head: str, prefix: str) -> set[str]:
        """Paths changed by checkpoint commits, excluding main-importing refresh commits."""
        workspace_files.validate_repo_path(prefix.rstrip("/"))
        output = self.run(
            "log",
            "--first-parent",
            "--format=",
            "--name-only",
            "--no-renames",
            "--fixed-strings",
            "--grep=Rotor-Operation: ",
            f"{base}..{head}",
            "--",
            prefix,
        )
        paths = {line for line in output.decode(errors="strict").splitlines() if line}
        for path in paths:
            workspace_files.validate_repo_path(path)
        return paths

    def merge_tree(self, base: str, ours: str, theirs: str) -> Merge:
        """Three-way merge of two commits' trees against an explicit base, with no worktree.

        Git chooses the merge base itself, so the sides are re-rooted onto a synthetic
        commit carrying the base tree inside this temporary object database. Conflicted
        files keep conflict markers in the returned tree.
        """
        self._identity("hatchery")

        def synthetic(revision: str, parent: str | None = None) -> str:
            parents = ["-p", parent] if parent else []
            return (
                self.run("commit-tree", f"{revision}^{{tree}}", *parents, data=b"merge input\n")
                .decode()
                .strip()
            )

        root = synthetic(base)
        refs = list(MERGE_LABELS)
        self.run("update-ref", refs[0], synthetic(ours, root))
        self.run("update-ref", refs[1], synthetic(theirs, root))
        result = self.command(
            "merge-tree", "--write-tree", "-z", "--name-only", refs[0], refs[1], check=False
        )
        if result.returncode not in (0, 1):
            raise GitError("Git merge-tree failed; the worker needs Git 2.38 or newer")
        parts = result.stdout.split(b"\0")
        tree = parts[0].decode()
        if not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise GitError("Git returned an invalid merged tree")
        conflicts = []
        for part in parts[1:]:
            if not part:
                break
            path = part.decode("utf-8", errors="strict")
            workspace_files.validate_repo_path(path)
            conflicts.append(path)
        return Merge(tree, tuple(conflicts))

    def replay(self, revision: str, operation: str) -> str | None:
        return (
            self.run(
                "log", "-1", "--format=%H", f"--grep=^Rotor-Operation: {operation}$", revision, "--"
            )
            .decode()
            .strip()
            or None
        )

    def acceptance_commits(self, base: str, head: str) -> tuple[str, ...]:
        """Acceptance commits on the first-parent path after `base`, oldest first."""
        commits = tuple(
            self.run(
                "log",
                "--first-parent",
                "--reverse",
                f"--max-count={MAX_EXTRA_PARENTS + 1}",
                "--format=%H",
                "--grep=^Rotor-Accept: v1$",
                f"{base}..{head}",
                "--",
            )
            .decode()
            .split()
        )
        if len(commits) > MAX_EXTRA_PARENTS:
            raise ValueError("accepted proposal ancestry exceeds limit")
        if any(not re.fullmatch(r"[0-9a-f]{40}", commit) for commit in commits):
            raise GitError("Git returned an invalid acceptance commit")
        return commits

    def _identity(self, owner: str) -> None:
        self.env.update(
            GIT_AUTHOR_NAME=f"hatchery/{owner}",
            GIT_AUTHOR_EMAIL=f"{owner}@hatchery.local",
            GIT_COMMITTER_NAME=f"hatchery/{owner}",
            GIT_COMMITTER_EMAIL=f"{owner}@hatchery.local",
        )

    def commit(
        self,
        base: str | None,
        changes: collections.abc.Mapping[str, workspace_files.File | None],
        *,
        prefixes: tuple[str, ...],
        owner: str,
        message: str,
        extra_parents: collections.abc.Sequence[str] = (),
    ) -> str:
        if len(extra_parents) > MAX_EXTRA_PARENTS:
            raise ValueError("commit parent count exceeds limit")
        if any(not re.fullmatch(r"[0-9a-f]{40}", parent) for parent in extra_parents):
            raise ValueError("extra parents must be Git commit SHAs")
        if len(set(extra_parents)) != len(extra_parents) or base in extra_parents:
            raise ValueError("commit parents must be unique")
        self.run("read-tree", base if base else "--empty")
        records = []
        # Remove first so a file can become a directory (and vice versa).
        for path in changes:
            workspace_files.validate_repo_path(path)
            if not path.startswith(prefixes):
                raise ValueError("workspace change is outside permitted scope")
            records.append(f"0 {'0' * 40}\t{path}\0".encode())
        values = [(path, value) for path, value in changes.items() if value is not None]
        oids = self.hash_objects([value.content for _, value in values], write=True)
        for (path, value), oid in zip(values, oids, strict=True):
            records.append(f"{'100755' if value.executable else '100644'} {oid}\t{path}\0".encode())
        self.run("update-index", "-z", "--index-info", data=b"".join(records))
        tree = self.run("write-tree").decode().strip()
        entries = self.entries(tree)
        self._validate_entries(
            {path: entry for path, entry in entries.items() if path.startswith(prefixes)}
        )
        if base:
            changed = self.run(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                base,
                tree,
            )
            if any(
                not path.decode().startswith(prefixes) or path.decode() not in changes
                for path in changed.split(b"\0")
                if path
            ):
                raise ValueError("resulting Git diff escaped permitted scope")
        self._identity(owner)
        parents = ["-p", base] if base else []
        for parent in extra_parents:
            parents.extend(["-p", parent])
        return self.run("commit-tree", tree, *parents, data=message.encode()).decode().strip()
