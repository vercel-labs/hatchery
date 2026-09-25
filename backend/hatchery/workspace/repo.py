"""Worker-only Git workspace: thread branches, checkpoints, and reviewed consolidation.

Paths as the agent sees them (`self/`, `wiki/`, `collective/<agent>/`) map onto the
repository layout (`agents/<agent>/`, `wiki/`). An `owner` here is the agent ID. No Git
metadata, token, or worker checkout is ever transferred into a sandbox.
"""

import asyncio
import collections.abc
import contextlib
import dataclasses
import hashlib
import logging
import re
import threading

from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git

SECTIONS = ("workspace", "wiki")
GIT_SESSION_OPERATIONS = 64
"""Operations a reused Git session serves before it is discarded."""
GIT_SESSION_BYTES = 64 * 1024 * 1024
"""Object-database size at which a reused Git session is discarded early."""


def section_prefixes(owner: str, section: str) -> tuple[str, str]:
    """The agent-view and repository prefixes of one consolidation section."""
    if section == "workspace":
        return "self/", f"agents/{owner}/"
    if section == "wiki":
        return "wiki/", "wiki/"
    raise ValueError("section must be workspace or wiki")


def is_serve_path(owner: str, path: str) -> bool:
    """Whether one agent-directory path deploys executable serving behavior."""
    prefix = f"agents/{owner}/"
    if not path.startswith(prefix):
        return False
    relative = path.removeprefix(prefix)
    return relative == "requirements.txt" or relative.startswith(("api/", "schedules/", "lib/"))


@dataclasses.dataclass(frozen=True)
class Proposal:
    owner: str
    section: str
    summary: str
    base: str
    sha: str
    changes: dict[str, workspace_files.File | None]
    process: str = ""
    thread: str = ""
    accepted: tuple[tuple[str, str], ...] = ()


@dataclasses.dataclass(frozen=True)
class Consolidation:
    """One section proposed to an upstream, or the paths Git could not merge."""

    section: str
    head: str
    upstream: str
    proposal: str = ""  # the proposal branch now carrying this section's contribution
    obsolete: str = ""  # an unmerged proposal whose contribution the thread has withdrawn
    conflicts: tuple[str, ...] = ()  # agent-view paths the thread must repair after a refresh


@dataclasses.dataclass(frozen=True)
class Refresh:
    """Current upstream merged into a thread branch; conflicted files carry markers."""

    sha: str
    upstream: str
    main: str
    files: dict[str, workspace_files.File]
    conflicts: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()  # clean upstream changes to files this thread also touched
    changed: tuple[str, ...] = ()  # every path changed in the effective thread view


@dataclasses.dataclass
class _GitSession:
    git: workspace_git.Git
    uses: int = 0
    busy: bool = False
    retired: bool = False  # close as soon as the current borrower returns it


type TokenProvider = collections.abc.Callable[[], collections.abc.Awaitable[str]]
type MainObserver = collections.abc.Callable[[str], collections.abc.Awaitable[None]]


class WorkspaceRepo:
    def __init__(
        self,
        remote: str,
        *,
        token: str | TokenProvider | None = None,
    ):
        workspace_git.parse_remote(remote)
        if isinstance(token, str):
            workspace_git.validate_text(token, "token", 16384)
        self.remote = remote
        self._token = token
        self._git_sessions: list[_GitSession] = []
        self._git_sessions_lock = threading.Lock()
        self._main_observer: MainObserver | None = None

    def set_main_observer(self, observer: MainObserver | None) -> None:
        """Install a best-effort async observer for authoritative main reads."""
        self._main_observer = observer

    async def _observe_main(self, revision: str) -> None:
        if self._main_observer is None:
            return
        try:
            await self._main_observer(revision)
        except Exception:
            logging.getLogger(__name__).exception(
                "main observation failed", extra={"revision": revision}
            )

    @property
    def has_credentials(self) -> bool:
        return self._token is not None

    async def credential(self) -> str | None:
        token = await self._token() if callable(self._token) else self._token
        if token is not None:
            workspace_git.validate_text(token, "token", 16384)
        return token

    async def _run[T](self, operation: collections.abc.Callable[[str | None], T]) -> T:
        return await asyncio.to_thread(operation, await self.credential())

    @contextlib.contextmanager
    def _git(self, token: str | None) -> collections.abc.Iterator[workspace_git.Git]:
        """Borrow an isolated Git session exclusively for one repository operation."""
        with self._git_sessions_lock:
            session = next(
                (candidate for candidate in self._git_sessions if not candidate.busy), None
            )
            if session is not None:
                session.busy = True
        if session is None:
            # `git init` runs outside the lock so unrelated operations are not serialized
            # on it; a second concurrent creator simply yields a second session.
            session = _GitSession(workspace_git.Git(self.remote, token), busy=True)
            with self._git_sessions_lock:
                self._git_sessions.append(session)
        try:
            session.git.set_token(token)
            yield session.git
        finally:
            # Measured outside the lock; only this borrower touches the session until it
            # is marked idle below.
            oversized = session.git.object_bytes() > GIT_SESSION_BYTES
            with self._git_sessions_lock:
                session.uses += 1
                session.busy = False
                close = session.retired or oversized or session.uses >= GIT_SESSION_OPERATIONS
                if close and session in self._git_sessions:
                    self._git_sessions.remove(session)
            if close:
                session.git.close()

    def close(self) -> None:
        """Release idle isolated object databases; busy ones close when their operation ends."""
        with self._git_sessions_lock:
            idle = [session for session in self._git_sessions if not session.busy]
            for session in self._git_sessions:
                session.retired = True
            self._git_sessions = [s for s in self._git_sessions if s.busy]
        for session in idle:
            session.git.close()

    @staticmethod
    def thread_branch(owner: str, process_id: str) -> str:
        workspace_git.validate_owner(owner)
        workspace_git.validate_text(process_id, "process_id")
        return f"threads/{owner}/{hashlib.sha256(process_id.encode()).hexdigest()}"

    def _thread(self, owner: str, branch: str) -> None:
        workspace_git.validate_owner(owner)
        if not re.fullmatch(rf"threads/{re.escape(owner)}/[0-9a-f]{{64}}", branch):
            raise ValueError("thread branch must belong to the workspace and use thread_branch()")

    def _upstream(self, owner: str, upstream: str, branch: str = "") -> None:
        if upstream == branch:
            raise ValueError("thread branch and upstream must be different")
        if upstream != "main":
            self._thread(owner, upstream)

    def _view(
        self, git: workspace_git.Git, owner: str, own: str, main: str
    ) -> dict[str, workspace_files.File]:
        entries = {}
        for path, entry in git.entries(own).items():
            if path.startswith(f"agents/{owner}/"):
                entries[f"self/{path.removeprefix(f'agents/{owner}/')}"] = entry
            elif path.startswith("wiki/"):
                entries[path] = entry
        for path, entry in git.entries(main).items():
            if path.startswith("agents/") and not path.startswith(f"agents/{owner}/"):
                entries[f"collective/{path.removeprefix('agents/')}"] = entry
        return git.read(entries)

    async def list_agents(self) -> list[str]:
        """Agent directories on main: the collective view, not the registry (the DB is)."""

        def work(token: str | None) -> tuple[str, list[str]]:
            with self._git(token) as git:
                main = git.fetch("main")
                assert main is not None
                owners = set()
                for path, entry in git.entries(main).items():
                    match = re.fullmatch(r"agents/([a-z0-9][a-z0-9_-]{0,63})/.+", path)
                    if match and entry.mode in ("100644", "100755"):
                        owners.add(match[1])
                return main, sorted(owners)

        revision, owners = await self._run(work)
        await self._observe_main(revision)
        return owners

    async def materialize(
        self,
        owner: str,
        branch: str,
        *,
        upstream: str = "main",
        upstream_sha: str = "",
    ) -> dict[str, workspace_files.File]:
        self._thread(owner, branch)
        self._upstream(owner, upstream, branch)
        if upstream_sha and not re.fullmatch(r"[0-9a-f]{40}", upstream_sha):
            raise ValueError("upstream_sha must be a Git commit SHA")

        def work(token: str | None) -> tuple[str, dict[str, workspace_files.File]]:
            with self._git(token) as git:
                for _ in range(workspace_git.MAX_ATTEMPTS):
                    branches = tuple(dict.fromkeys(("main", upstream, branch)))
                    fetched = git.fetch_many(branches, optional=(branch,))
                    main, upstream_tip, thread = (
                        fetched["main"],
                        fetched[upstream],
                        fetched[branch],
                    )
                    assert main is not None and upstream_tip is not None
                    if upstream_sha and not git.is_ancestor(upstream_sha, upstream_tip):
                        raise ValueError("upstream_sha is not an ancestor of upstream")
                    if thread is None:
                        start = upstream_sha or upstream_tip
                        if not git.push(start, branch, None):
                            continue
                        thread = start
                    elif upstream_sha and not git.forks_at(thread, upstream_tip, upstream_sha):
                        raise ValueError("existing thread branch does not fork at upstream_sha")
                    return main, self._view(git, owner, thread, main)
            raise workspace_git.GitError(
                "could not create thread branch after bounded push retries"
            )

        revision, tree = await self._run(work)
        await self._observe_main(revision)
        return tree

    async def thread_base(self, owner: str, branch: str, *, upstream: str = "main") -> str:
        """Return the upstream merge base from which a thread branch derives."""
        self._thread(owner, branch)
        self._upstream(owner, upstream, branch)

        def work(token: str | None) -> tuple[str, str]:
            with self._git(token) as git:
                for _ in range(workspace_git.MAX_ATTEMPTS):
                    branches = tuple(dict.fromkeys((upstream, branch)))
                    fetched = git.fetch_many(branches, optional=(branch,))
                    upstream_tip, thread = fetched[upstream], fetched[branch]
                    assert upstream_tip is not None
                    if thread is None:
                        if not git.push(upstream_tip, branch, None):
                            continue
                        thread = upstream_tip
                    return upstream_tip, git.merge_base(upstream_tip, thread)
            raise workspace_git.GitError(
                "could not create thread branch after bounded push retries"
            )

        revision, base = await self._run(work)
        if upstream == "main":
            await self._observe_main(revision)
        return base

    async def thread_head(self, owner: str, branch: str) -> str:
        """Return the current validated thread ref for cleanup fencing."""
        self._thread(owner, branch)

        def work(token: str | None) -> str:
            with self._git(token) as git:
                head = git.fetch(branch)
                assert head is not None
                return head

        return await self._run(work)

    async def checkpoint(
        self,
        owner: str,
        branch: str,
        files: collections.abc.Mapping[str, workspace_files.File],
        *,
        process_id: str,
        operation_id: str,
    ) -> str:
        self._thread(owner, branch)
        operation = workspace_git.operation_digest(owner, process_id, operation_id, "checkpoint")
        snapshot = {
            path: file for path, file in files.items() if not workspace_files.is_python_cache(path)
        }
        workspace_files.validate_tree(snapshot)
        mapped = {}
        for path, value in snapshot.items():
            if path.startswith("self/"):
                mapped[f"agents/{owner}/{path[5:]}"] = value
            elif path.startswith("wiki/"):
                mapped[path] = value
            elif not path.startswith("collective/") or len(path.split("/")) < 3:
                raise ValueError(
                    "checkpoint accepts only self/, wiki/, and collective/<owner>/ files"
                )
        workspace_files.validate_tree(mapped)

        def work(token: str | None) -> str:
            prefixes = (f"agents/{owner}/", "wiki/")
            with self._git(token) as git:
                for _ in range(workspace_git.MAX_ATTEMPTS):
                    fetched = git.fetch_many(("main", branch), optional=(branch,))
                    head = fetched[branch]
                    if head is None:
                        head = fetched["main"]
                        assert head is not None
                        if not git.push(head, branch, None):
                            continue
                    replay = git.replay(head, operation)
                    if replay:
                        return replay
                    changes: dict[str, workspace_files.File | None] = {
                        path: None for path in git.entries(head) if path.startswith(prefixes)
                    }
                    changes.update(mapped)
                    sha = git.commit(
                        head,
                        changes,
                        prefixes=prefixes,
                        owner=owner,
                        message=(
                            f"Checkpoint {owner}\n\nRotor-Process: {process_id}\n"
                            f"Rotor-Operation: {operation}\n"
                        ),
                    )
                    if git.push(sha, branch, head):
                        return sha
            raise workspace_git.GitError("checkpoint push failed after bounded retries")

        return await self._run(work)

    async def read_main(self, owner: str) -> tuple[str, dict[str, workspace_files.File]]:
        workspace_git.validate_owner(owner)

        def work(token: str | None) -> tuple[str, dict[str, workspace_files.File]]:
            with self._git(token) as git:
                main = git.fetch("main")
                assert main is not None
                return main, self._view(git, owner, main, main)

        outcome = await self._run(work)
        await self._observe_main(outcome[0])
        return outcome

    async def read_owner_main(self, owner: str) -> tuple[str, dict[str, workspace_files.File]]:
        """Read only one owner's workspace from one fetched main revision."""
        workspace_git.validate_owner(owner)

        def work(token: str | None) -> tuple[str, dict[str, workspace_files.File]]:
            with self._git(token) as git:
                main = git.fetch("main")
                assert main is not None
                prefix = f"agents/{owner}/"
                entries = {
                    f"self/{path.removeprefix(prefix)}": entry
                    for path, entry in git.entries(main).items()
                    if path.startswith(prefix)
                }
                if not entries:
                    raise FileNotFoundError(f"workspace {owner!r} does not exist on main")
                return main, git.read(entries)

        outcome = await self._run(work)
        await self._observe_main(outcome[0])
        return outcome

    async def read_schedule_main(self) -> tuple[str, dict[str, dict[str, workspace_files.File]]]:
        """Read every owner workspace from one fetched revision for schedule reconciliation."""

        def work(token: str | None) -> tuple[str, dict[str, dict[str, workspace_files.File]]]:
            with self._git(token) as git:
                main = git.fetch("main")
                assert main is not None
                entries = git.entries(main)
                owners = sorted(
                    {
                        match.group(1)
                        for path, entry in entries.items()
                        if (match := re.fullmatch(r"agents/([a-z0-9][a-z0-9_-]{0,63})/.+", path))
                        and entry.mode in ("100644", "100755")
                    }
                )
                trees: dict[str, dict[str, workspace_files.File]] = {}
                for owner in owners:
                    prefix = f"agents/{owner}/"
                    selected = {
                        f"self/{path.removeprefix(prefix)}": entry
                        for path, entry in entries.items()
                        if re.fullmatch(rf"{re.escape(prefix)}schedules/[^/]+/job\.py", path)
                        and entry.mode in ("100644", "100755")
                    }
                    trees[owner] = git.read(selected)
                return main, trees

        return await self._run(work)

    async def main_revision(self) -> str:
        """Fetch and return current main without materializing workspace files."""

        def work(token: str | None) -> str:
            with self._git(token) as git:
                main = git.fetch("main")
                assert main is not None
                return main

        return await self._run(work)

    async def read_owner_at(self, owner: str, sha: str) -> dict[str, workspace_files.File]:
        """Read only one owner's files at a commit that remains reachable from main."""
        workspace_git.validate_owner(owner)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("sha must be a Git commit SHA")

        def work(token: str | None) -> dict[str, workspace_files.File]:
            with self._git(token) as git:
                git.fetch("main")
                revision = git.resolve_commit(sha)
                prefix = f"agents/{owner}/"
                selected = {
                    f"self/{path.removeprefix(prefix)}": entry
                    for path, entry in git.entries(revision).items()
                    if path.startswith(prefix)
                }
                if not selected:
                    raise FileNotFoundError(f"workspace {owner!r} does not exist at {sha}")
                return git.read(selected)

        return await self._run(work)

    async def read_at(self, owner: str, sha: str) -> dict[str, workspace_files.File]:
        """Read the owner's complete view at a commit reachable from main."""
        workspace_git.validate_owner(owner)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("sha must be a Git commit SHA")

        def work(token: str | None) -> dict[str, workspace_files.File]:
            with self._git(token) as git:
                git.fetch("main")
                revision = git.resolve_commit(sha)
                return self._view(git, owner, revision, revision)

        return await self._run(work)

    def _section_base(
        self,
        git: workspace_git.Git,
        owner: str,
        section: str,
        process_id: str,
        upstream: str,
        head: str,
    ) -> str:
        """The commit both sides of a section merge derive from.

        Normally the fork point or the last refresh. When this thread's own `section`
        contribution has already reached upstream more recently, that thread commit is the
        base instead, so re-editing a published file never conflicts with itself.
        """
        base = git.merge_base(upstream, head)
        merged = git.merged_thread_head(upstream, process_id, section)
        if merged and git.is_ancestor(merged, head) and not git.is_ancestor(merged, base):
            return merged
        return base

    def _merge_section(
        self, git: workspace_git.Git, owner: str, section: str, upstream: str, head: str, base: str
    ) -> tuple[str, list[str]]:
        """Three-way merge one section of `head` into `upstream`."""
        _, target = section_prefixes(owner, section)
        merge = git.merge_tree(*git.project((base, upstream, head), target))
        conflicts = [path for path in merge.conflicts if path.startswith(target)]
        if len(conflicts) > workspace_files.MAX_FILES:
            raise ValueError("merge conflict count exceeds limit")
        return merge.tree, conflicts

    async def consolidate(
        self,
        owner: str,
        branch: str,
        section: str,
        *,
        head: str,
        process_id: str,
        summary: str,
        upstream: str = "main",
    ) -> Consolidation:
        """Merge one section of a thread commit into its upstream proposal branch.

        A thread keeps one proposal branch per section and rewrites it under a ref lease
        as its checkpoints advance, so review follows one evolving proposal. Paths Git
        cannot merge are returned unpublished; the thread repairs them after a refresh.
        """
        self._thread(owner, branch)
        self._upstream(owner, upstream, branch)
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("head must be a Git commit SHA")
        workspace_git.validate_text(summary, "summary")
        source, target = section_prefixes(owner, section)
        operation = workspace_git.operation_digest(owner, process_id, section, "proposal")
        proposal = f"consolidations/{owner}/{section}/{operation}"

        def work(token: str | None) -> Consolidation:
            with self._git(token) as git:
                for _ in range(workspace_git.MAX_ATTEMPTS):
                    branches = tuple(dict.fromkeys((upstream, branch, proposal)))
                    fetched = git.fetch_many(branches, optional=(proposal,))
                    upstream_tip, tip, existing = (
                        fetched[upstream],
                        fetched[branch],
                        fetched[proposal],
                    )
                    assert upstream_tip is not None and tip is not None
                    if not git.is_ancestor(head, tip):
                        raise ValueError("head is not a commit on the thread branch")
                    outcome = Consolidation(section, head, upstream_tip)
                    open_proposal = (
                        existing
                        if existing and not git.is_ancestor(existing, upstream_tip)
                        else None
                    )
                    base = self._section_base(git, owner, section, process_id, upstream_tip, head)
                    accepted = self._accepted_proposals(git, owner, branch, section, base, head)
                    changes: dict[str, workspace_files.File | None] = {}
                    if git.subtree(head, target) != git.subtree(base, target):
                        tree, conflicted = self._merge_section(
                            git, owner, section, upstream_tip, head, base
                        )
                        changes = self._tree_changes(
                            git, git.entries(upstream_tip), git.entries(tree), (target,)
                        )
                        unresolved = set(conflicted) | {
                            path
                            for path, file in changes.items()
                            if file and self._has_refresh_markers(file.content)
                        }
                        if unresolved:
                            paths = sorted(
                                source + path.removeprefix(target) for path in unresolved
                            )
                            return dataclasses.replace(outcome, conflicts=tuple(paths))
                    if not changes and not accepted:
                        return dataclasses.replace(
                            outcome, obsolete=proposal if open_proposal else ""
                        )
                    accepted_trailers = "".join(
                        f"Rotor-Accepted: {accepted_branch} {accepted_sha}\n"
                        for accepted_branch, accepted_sha in accepted
                    )
                    message = (
                        f"{summary}\n\nRotor-Owner: {owner}\nRotor-Section: {section}\n"
                        f"Rotor-Base: {upstream_tip}\nRotor-Process: {process_id}\n"
                        f"Rotor-Operation: {operation}\nRotor-Thread: {head}\n"
                        f"{accepted_trailers}"
                    )
                    sha = git.commit(
                        upstream_tip,
                        changes,
                        prefixes=(target,),
                        owner=owner,
                        message=message,
                        extra_parents=tuple(accepted_sha for _, accepted_sha in accepted),
                    )
                    if (
                        open_proposal
                        and git.entries(open_proposal) == git.entries(sha)
                        and git.commit_parents(open_proposal) == git.commit_parents(sha)
                        and git.commit_message(open_proposal) == git.commit_message(sha)
                    ):
                        return dataclasses.replace(
                            outcome, proposal=proposal
                        )  # a redelivered activation
                    if git.push(sha, proposal, existing):
                        return dataclasses.replace(outcome, proposal=proposal)
            raise workspace_git.GitError("proposal push failed after bounded retries")

        outcome = await self._run(work)
        if upstream == "main":
            await self._observe_main(outcome.upstream)
        return outcome

    async def refresh(
        self,
        owner: str,
        branch: str,
        *,
        head: str,
        base: str,
        process_id: str,
        upstream: str = "main",
    ) -> Refresh:
        """Merge current upstream into the thread branch at `head`.

        Clean merges install silently. Files Git cannot merge are committed carrying
        `main`/`thread` or `parent`/`thread` markers for the thread to repair.
        """
        self._thread(owner, branch)
        self._upstream(owner, upstream, branch)
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, base)):
            raise ValueError("head and base must be Git commit SHAs")

        def work(token: str | None) -> Refresh:
            with self._git(token) as git:
                branches = tuple(dict.fromkeys(("main", upstream, branch)))
                fetched = git.fetch_many(branches)
                main, upstream_tip, tip = fetched["main"], fetched[upstream], fetched[branch]
                assert main is not None and upstream_tip is not None and tip is not None
                if tip != head:
                    raise workspace_git.MainChanged(
                        "thread advanced; checkpoint again before refreshing"
                    )
                if not git.is_ancestor(base, head):
                    raise ValueError("base is not an ancestor of the thread head")
                if git.is_ancestor(upstream_tip, head):
                    return Refresh(head, upstream_tip, main, self._view(git, owner, head, main))
                changes: dict[str, workspace_files.File | None] = {}
                conflicts = []
                updated: list[str] = []
                changed: list[str] = []
                for section in SECTIONS:
                    source, target = section_prefixes(owner, section)
                    section_base = self._section_base(
                        git, owner, section, process_id, upstream_tip, head
                    )
                    head_entries = git.entries(head)
                    tree, conflicted = self._merge_section(
                        git, owner, section, upstream_tip, head, section_base
                    )
                    section_changes = self._tree_changes(
                        git, head_entries, git.entries(tree), (target,)
                    )
                    changes.update(section_changes)
                    changed.extend(
                        source + path.removeprefix(target).split("~", 1)[0]
                        for path in section_changes
                    )
                    thread_paths = git.checkpoint_paths(base, head, target)
                    updated.extend(
                        source + path.removeprefix(target)
                        for path in sorted(
                            (thread_paths & section_changes.keys()) - set(conflicted)
                        )
                    )
                    for path in conflicted:
                        file = changes.get(path)
                        if file is not None:
                            content = workspace_git.relabel_markers(file.content)
                            if upstream != "main":
                                content = content.replace(b"<<<<<<< main", b"<<<<<<< parent")
                                content = content.replace(b">>>>>>> main", b">>>>>>> parent")
                            changes[path] = workspace_files.File(content, file.executable)
                        conflicts.append(source + path.removeprefix(target).split("~", 1)[0])
                prefixes = (f"agents/{owner}/", "wiki/")
                sha = git.commit(
                    head,
                    changes,
                    prefixes=prefixes,
                    owner=owner,
                    message=f"Refresh {owner} from {'main' if upstream == 'main' else 'parent'}\n",
                    extra_parents=(upstream_tip,),
                )
                if not git.push(sha, branch, head):
                    raise workspace_git.MainChanged("thread advanced while refreshing")
                return Refresh(
                    sha,
                    upstream_tip,
                    main,
                    self._view(git, owner, sha, main),
                    tuple(sorted(conflicts)),
                    tuple(updated),
                    tuple(sorted(set(changed))),
                )

        outcome = await self._run(work)
        await self._observe_main(outcome.main)
        return outcome

    @staticmethod
    def _has_refresh_markers(content: bytes) -> bool:
        if workspace_git.has_markers(content):
            return True
        lines = content.split(b"\n")
        return b"<<<<<<< parent" in lines and b">>>>>>> thread" in lines

    @staticmethod
    def _tree_changes(
        git: workspace_git.Git,
        before: collections.abc.Mapping[str, workspace_git.Entry],
        after: collections.abc.Mapping[str, workspace_git.Entry],
        prefixes: tuple[str, ...],
    ) -> dict[str, workspace_files.File | None]:
        paths = [
            path
            for path in before.keys() | after.keys()
            if path.startswith(prefixes) and before.get(path) != after.get(path)
        ]
        added = {path: after[path] for path in paths if path in after}
        files = git.read(added)
        return {path: files.get(path) for path in paths}

    @staticmethod
    def _consolidation_branch(branch: str) -> re.Match[str]:
        match = re.fullmatch(
            r"consolidations/([a-z0-9][a-z0-9_-]{0,63})/(workspace|wiki)/([0-9a-f]{64})", branch
        )
        if not match:
            raise ValueError("not a consolidation branch")
        return match

    async def delete_proposal(self, branch: str, *, upstream: str = "main") -> bool:
        """Remove an unmerged proposal branch; merged or already-absent branches are left alone."""
        owner = self._consolidation_branch(branch).group(1)
        self._upstream(owner, upstream)

        def work(token: str | None) -> bool:
            with self._git(token) as git:
                refs = tuple(dict.fromkeys((upstream, branch)))
                fetched = git.fetch_many(refs, optional=(branch,))
                upstream_tip, sha = fetched[upstream], fetched[branch]
                assert upstream_tip is not None
                if sha is None or git.is_ancestor(sha, upstream_tip):
                    return False
                return git.delete(branch, sha)

        return await self._run(work)

    @staticmethod
    def _trailers(message: str) -> list[tuple[str, str]]:
        paragraphs = message.rstrip("\n").split("\n\n")
        if len(paragraphs) < 2:
            return []
        trailers = []
        for line in paragraphs[-1].splitlines():
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*): (.+)", line)
            if not match:
                return []
            trailers.append((match.group(1), match.group(2)))
        return trailers

    def _proposal(
        self,
        git: workspace_git.Git,
        branch: str,
        sha: str,
        *,
        _stack: frozenset[tuple[str, str]] = frozenset(),
        _cache: dict[tuple[str, str], Proposal] | None = None,
        _visited: set[tuple[str, str]] | None = None,
    ) -> Proposal:
        cache = {} if _cache is None else _cache
        visited = set() if _visited is None else _visited
        key = (branch, sha)
        if key in cache:
            return cache[key]
        if key in _stack:
            raise ValueError("proposal audit ancestry is recursive")
        if key not in visited:
            # One proposal plus the maximum number of direct extra parents is valid.
            if len(visited) > workspace_git.MAX_EXTRA_PARENTS:
                raise ValueError("proposal audit ancestry exceeds limit")
            visited.add(key)

        match = self._consolidation_branch(branch)
        owner: str = match.group(1)
        section: str = match.group(2)
        branch_operation: str = match.group(3)
        try:
            message = git.commit_message(sha).decode()
        except UnicodeDecodeError as exc:
            raise ValueError("proposal commit message is not UTF-8") from exc
        trailers = self._trailers(message)
        scalar_keys = {
            "Rotor-Owner",
            "Rotor-Section",
            "Rotor-Base",
            "Rotor-Process",
            "Rotor-Operation",
            "Rotor-Thread",
        }
        if any(key not in scalar_keys | {"Rotor-Accepted"} for key, _ in trailers):
            raise ValueError("proposal has an unknown audit trailer")
        metadata = {
            key: trailer_values[0]
            for key in scalar_keys
            if len(trailer_values := [value for name, value in trailers if name == key]) == 1
        }
        accepted_values = [value for key, value in trailers if key == "Rotor-Accepted"]
        base = metadata.get("Rotor-Base", "")
        process = metadata.get("Rotor-Process", "")
        thread = metadata.get("Rotor-Thread", "")
        try:
            expected_operation = workspace_git.operation_digest(owner, process, section, "proposal")
        except ValueError as exc:
            raise ValueError("proposal has an invalid process audit record") from exc

        accepted: list[tuple[str, str]] = []
        for value in accepted_values:
            fields = value.split()
            if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[1]):
                raise ValueError("proposal has an invalid accepted-proposal audit record")
            self._consolidation_branch(fields[0])
            accepted.append((fields[0], fields[1]))
        if len(accepted) > workspace_git.MAX_EXTRA_PARENTS or len(set(accepted)) != len(accepted):
            raise ValueError("proposal accepted-proposal ancestry is invalid or exceeds limit")

        parents = git.commit_parents(sha)
        if (
            len(metadata) != len(scalar_keys)
            or metadata.get("Rotor-Owner") != owner
            or metadata.get("Rotor-Section") != section
            or metadata.get("Rotor-Operation") != expected_operation
            or branch_operation != expected_operation
            or not re.fullmatch(r"[0-9a-f]{40}", base)
            or not re.fullmatch(r"[0-9a-f]{40}", thread)
            or thread == "0" * 40
            or parents != (base, *(accepted_sha for _, accepted_sha in accepted))
        ):
            raise ValueError("proposal does not have a valid upstream-parent audit record")
        before, after = git.entries(base), git.entries(sha)
        changed = {
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        }
        _, prefix = section_prefixes(owner, section)
        if any(not path.startswith(prefix) for path in changed):
            raise ValueError("proposal diff escaped its declared scope")
        if len(changed) > workspace_files.MAX_FILES:
            raise ValueError("proposal change count exceeds limit")
        files = git.read({path: entry for path, entry in after.items() if path.startswith(prefix)})
        for path in changed:
            workspace_files.validate_repo_path(path)
        summary = message.splitlines()[0] if message.splitlines() else ""
        workspace_git.validate_text(summary, "proposal summary")
        source_tip = git.fetch(self.thread_branch(owner, process))
        assert source_tip is not None
        if not git.is_ancestor(thread, source_tip):
            raise ValueError("proposal thread commit is not on its declared process branch")
        section_base = self._section_base(git, owner, section, process, base, thread)
        expected_tree, conflicts = self._merge_section(
            git, owner, section, base, thread, section_base
        )
        if conflicts or git.subtree(sha, prefix) != git.subtree(expected_tree, prefix):
            raise ValueError("proposal diff does not match its declared thread commit")
        expected_accepted = self._accepted_proposals(
            git,
            owner,
            self.thread_branch(owner, process),
            section,
            section_base,
            thread,
            _stack=_stack | {key},
            _cache=cache,
            _visited=visited,
        )
        if tuple(accepted) != expected_accepted:
            raise ValueError("proposal accepted-proposal audit does not match its thread ancestry")
        proposal = Proposal(
            owner=owner,
            section=section,
            summary=summary,
            base=base,
            sha=sha,
            changes={path: files.get(path) for path in changed},
            process=process,
            thread=thread,
            accepted=tuple(accepted),
        )
        cache[key] = proposal
        return proposal

    @staticmethod
    def _accept_operation(owner: str, branch: str, task_id: str, proposal_sha: str) -> str:
        return workspace_git.operation_digest(owner, branch, f"{task_id}:{proposal_sha}", "accept")

    def _accepted_commit(
        self,
        git: workspace_git.Git,
        owner: str,
        parent_branch: str,
        revision: str,
        *,
        _stack: frozenset[tuple[str, str]] = frozenset(),
        _cache: dict[tuple[str, str], Proposal] | None = None,
        _visited: set[tuple[str, str]] | None = None,
    ) -> tuple[str, str, str, str, str, Proposal] | None:
        try:
            message = git.commit_message(revision).decode()
        except UnicodeDecodeError as exc:
            raise ValueError("acceptance commit message is not UTF-8") from exc
        trailers = self._trailers(message)
        if ("Rotor-Accept", "v1") not in trailers:
            return None
        keys = {
            "Rotor-Accept",
            "Rotor-Operation",
            "Rotor-Task",
            "Rotor-Child",
            "Hatchery-Proposal",
            "Hatchery-Proposal-SHA",
        }
        if len(trailers) != len(keys) or {key for key, _ in trailers} != keys:
            raise ValueError("acceptance commit has an invalid audit record")
        metadata = dict(trailers)
        task_id = metadata["Rotor-Task"]
        child_process_id = metadata["Rotor-Child"]
        proposal_branch = metadata["Hatchery-Proposal"]
        proposal_sha = metadata["Hatchery-Proposal-SHA"]
        workspace_git.validate_text(task_id, "task_id", 512)
        workspace_git.validate_text(child_process_id, "child_process_id")
        self._consolidation_branch(proposal_branch)
        if not re.fullmatch(r"[0-9a-f]{40}", proposal_sha):
            raise ValueError("acceptance commit has an invalid proposal SHA")
        proposal = self._proposal(
            git,
            proposal_branch,
            proposal_sha,
            _stack=_stack,
            _cache=_cache,
            _visited=_visited,
        )
        parents = git.commit_parents(revision)
        operation = self._accept_operation(owner, parent_branch, task_id, proposal_sha)
        if (
            metadata["Rotor-Accept"] != "v1"
            or metadata["Rotor-Operation"] != operation
            or proposal.process != child_process_id
            or proposal.owner != owner
            or len(parents) != 2
            or parents[1] != proposal_sha
            or not git.is_ancestor(proposal.base, parents[0])
        ):
            raise ValueError("acceptance commit has an invalid proposal binding")
        _, target = section_prefixes(owner, proposal.section)
        merged_tree, conflicts = self._merge_section(
            git, owner, proposal.section, parents[0], proposal_sha, proposal.base
        )
        if conflicts:
            raise ValueError("acceptance commit replays a conflicted proposal")
        parent_entries = git.entries(parents[0])
        merged_entries = git.entries(merged_tree)
        accepted_entries = git.entries(revision)
        paths = parent_entries.keys() | merged_entries.keys() | accepted_entries.keys()
        if any(
            accepted_entries.get(path)
            != (merged_entries.get(path) if path.startswith(target) else parent_entries.get(path))
            for path in paths
        ):
            raise ValueError("acceptance commit tree does not match its proposal merge")
        return proposal_branch, proposal_sha, task_id, child_process_id, operation, proposal

    def _accepted_proposals(
        self,
        git: workspace_git.Git,
        owner: str,
        parent_branch: str,
        section: str,
        base: str,
        head: str,
        *,
        _stack: frozenset[tuple[str, str]] = frozenset(),
        _cache: dict[tuple[str, str], Proposal] | None = None,
        _visited: set[tuple[str, str]] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        accepted = []
        for revision in git.acceptance_commits(base, head):
            record = self._accepted_commit(
                git,
                owner,
                parent_branch,
                revision,
                _stack=_stack,
                _cache=_cache,
                _visited=_visited,
            )
            if record is None:
                raise ValueError("acceptance audit search returned a non-acceptance commit")
            proposal_branch, proposal_sha, _, _, _, proposal = record
            if proposal.section == section:
                accepted.append((proposal_branch, proposal_sha))
        if len(set(accepted)) != len(accepted):
            raise ValueError("proposal was accepted more than once in the section ancestry")
        return tuple(accepted)

    async def inspect_proposal(self, branch: str) -> Proposal:
        # Validate before placing an untrusted branch in a Git refspec.
        self._consolidation_branch(branch)

        def work(token: str | None) -> Proposal:
            with self._git(token) as git:
                sha = git.fetch(branch)
                assert sha is not None
                return self._proposal(git, branch, sha)

        return await self._run(work)

    async def merged_proposals(
        self, branches: list[str], *, upstream: str = "main"
    ) -> dict[str, bool]:
        """Current Git ancestry is authoritative even after a thread has stopped."""
        owners = set()
        for branch in branches:
            owners.add(self._consolidation_branch(branch).group(1))
        if upstream != "main":
            if len(owners) != 1:
                raise ValueError("a thread upstream requires proposals from one workspace")
            self._upstream(next(iter(owners)), upstream)

        def work(token: str | None) -> dict[str, bool]:
            merged = {}
            with self._git(token) as git:
                unique = tuple(dict.fromkeys(branches))
                refs = tuple(dict.fromkeys((upstream, *unique)))
                fetched = git.fetch_many(refs, optional=unique)
                upstream_tip = fetched[upstream]
                assert upstream_tip is not None
                for branch in unique:
                    sha = fetched[branch]
                    merged[branch] = sha is not None and git.is_ancestor(sha, upstream_tip)
            return merged

        if not branches:
            return {}
        result = await self._run(work)
        if upstream == "main":
            # The public result intentionally contains no SHA.
            await self._observe_current_main()
        return result

    async def _observe_current_main(self) -> None:
        revision = await self.main_revision()
        await self._observe_main(revision)

    async def merge(self, branch: str, *, expected_sha: str | None = None) -> str:
        await self.inspect_proposal(branch)

        def work(token: str | None) -> str:
            with self._git(token) as git:
                for _ in range(workspace_git.MAX_ATTEMPTS):
                    fetched = git.fetch_many((branch, "main"))
                    head, main = fetched[branch], fetched["main"]
                    assert head is not None and main is not None
                    if expected_sha is not None and head != expected_sha:
                        raise workspace_git.MainChanged(
                            "Proposal changed; refresh and review its latest diff"
                        )
                    proposal = self._proposal(git, branch, head)
                    if not git.is_ancestor(proposal.base, main):
                        raise ValueError("proposal base is not reachable from main")
                    if git.is_ancestor(head, main):
                        return main
                    _, prefix = section_prefixes(proposal.owner, proposal.section)
                    tree, conflicts = self._merge_section(
                        git,
                        proposal.owner,
                        proposal.section,
                        main,
                        head,
                        proposal.base,
                    )
                    if conflicts:
                        paths = ", ".join(path.split("~", 1)[0] for path in sorted(conflicts))
                        if any("~" in path for path in conflicts):
                            raise workspace_git.MergeConflict(
                                f"main changed the file/directory structure at {paths}"
                            )
                        raise workspace_git.MergeConflict(f"main and proposal both changed {paths}")
                    current, merged = git.entries(main), git.entries(tree)
                    changes = self._tree_changes(git, current, merged, (prefix,))
                    sha = git.commit(
                        main,
                        changes,
                        prefixes=(prefix,),
                        owner=proposal.owner,
                        message=(
                            f"Merge {proposal.section}: {proposal.summary}\n\n"
                            f"Hatchery-Proposal: {branch}\n"
                        ),
                        extra_parents=(head,),
                    )
                    if git.push(sha, "main", main):
                        return sha
            raise workspace_git.GitError("main merge push failed after bounded retries")

        sha = await self._run(work)
        await self._observe_main(sha)
        return sha

    async def accept(
        self,
        owner: str,
        branch: str,
        proposal_branch: str,
        *,
        head: str,
        proposal_sha: str,
        child_process_id: str,
        task_id: str,
    ) -> Refresh:
        """Merge a child's scoped proposal into its parent thread branch."""
        self._thread(owner, branch)
        self._consolidation_branch(proposal_branch)
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, proposal_sha)):
            raise ValueError("head and proposal_sha must be Git commit SHAs")
        workspace_git.validate_text(child_process_id, "child_process_id")
        workspace_git.validate_text(task_id, "task_id", 512)
        child_branch = self.thread_branch(owner, child_process_id)
        if child_branch == branch:
            raise ValueError("accepted proposal must come from a child thread")
        operation = self._accept_operation(owner, branch, task_id, proposal_sha)

        def work(token: str | None) -> Refresh:
            with self._git(token) as git:
                refs = tuple(dict.fromkeys(("main", branch, proposal_branch, child_branch)))
                fetched = git.fetch_many(refs)
                main, tip, fetched_proposal, child_tip = (
                    fetched["main"],
                    fetched[branch],
                    fetched[proposal_branch],
                    fetched[child_branch],
                )
                assert (
                    main is not None
                    and tip is not None
                    and fetched_proposal is not None
                    and child_tip is not None
                )
                if tip != head:
                    try:
                        replay = self._accepted_commit(git, owner, branch, tip)
                    except ValueError:
                        replay = None
                    if (
                        replay is not None
                        and replay[:5]
                        == (
                            proposal_branch,
                            proposal_sha,
                            task_id,
                            child_process_id,
                            operation,
                        )
                        and git.commit_parents(tip) == (head, proposal_sha)
                    ):
                        proposal = replay[5]
                        if not git.is_ancestor(proposal.thread, child_tip):
                            raise ValueError(
                                "proposal thread commit is not on the expected child branch"
                            )
                        source, target = section_prefixes(owner, proposal.section)
                        changes = self._tree_changes(
                            git, git.entries(head), git.entries(tip), (target,)
                        )
                        changed = tuple(
                            sorted(source + path.removeprefix(target) for path in changes)
                        )
                        return Refresh(
                            tip,
                            proposal_sha,
                            main,
                            self._view(git, owner, tip, main),
                            changed=changed,
                        )
                    raise workspace_git.MainChanged(
                        "thread advanced; checkpoint again before accepting"
                    )
                if fetched_proposal != proposal_sha:
                    raise ValueError("proposal changed; review its latest diff before accepting")
                proposal = self._proposal(git, proposal_branch, proposal_sha)
                if proposal.owner != owner:
                    raise ValueError("proposal does not belong to the parent workspace")
                if proposal.process != child_process_id:
                    raise ValueError("proposal does not belong to the expected child process")
                if not git.is_ancestor(proposal.thread, child_tip):
                    raise ValueError("proposal thread commit is not on the expected child branch")
                source, target = section_prefixes(owner, proposal.section)
                if not git.is_ancestor(proposal.base, head):
                    raise ValueError("proposal base is not reachable from the parent thread")
                if git.is_ancestor(proposal_sha, head):
                    raise ValueError("proposal is already present in the parent history")

                tree, conflicts = self._merge_section(
                    git,
                    owner,
                    proposal.section,
                    head,
                    proposal_sha,
                    proposal.base,
                )
                if conflicts:
                    paths = tuple(
                        sorted(
                            source + path.removeprefix(target).split("~", 1)[0]
                            for path in conflicts
                        )
                    )
                    joined = ", ".join(paths)
                    if any("~" in path for path in conflicts):
                        raise workspace_git.MergeConflict(
                            f"parent changed the file/directory structure at {joined}"
                        )
                    raise workspace_git.MergeConflict(f"parent and proposal both changed {joined}")

                current, merged = git.entries(head), git.entries(tree)
                changes = self._tree_changes(git, current, merged, (target,))
                sha = git.commit(
                    head,
                    changes,
                    prefixes=(target,),
                    owner=owner,
                    message=(
                        f"Merge {proposal.section} from task {task_id}: {proposal.summary}\n\n"
                        f"Rotor-Accept: v1\nRotor-Operation: {operation}\n"
                        f"Rotor-Task: {task_id}\nRotor-Child: {child_process_id}\n"
                        f"Hatchery-Proposal: {proposal_branch}\n"
                        f"Hatchery-Proposal-SHA: {proposal_sha}\n"
                    ),
                    extra_parents=(proposal_sha,),
                )
                if not git.push(sha, branch, head):
                    raise workspace_git.MainChanged("thread advanced while accepting proposal")
                changed = tuple(sorted(source + path.removeprefix(target) for path in changes))
                return Refresh(
                    sha,
                    proposal_sha,
                    main,
                    self._view(git, owner, sha, main),
                    changed=changed,
                )

        outcome = await self._run(work)
        await self._observe_main(outcome.main)
        return outcome

    async def join(
        self, owner: str, workspace: collections.abc.Mapping[str, workspace_files.File]
    ) -> str:
        """Create `agents/<owner>/` on main from a template tree (paths relative to it)."""
        workspace_git.validate_owner(owner)
        if "AGENTS.md" not in workspace:
            raise ValueError("an agent needs an AGENTS.md")
        files = {f"agents/{owner}/{path}": file for path, file in workspace.items()}
        workspace_files.validate_tree(files)

        def work(token: str | None) -> str:
            with self._git(token) as git:
                attempted = None
                for _ in range(workspace_git.MAX_ATTEMPTS):
                    main = git.fetch("main")
                    assert main is not None
                    if attempted and git.is_ancestor(attempted, main):
                        return attempted
                    prefix = f"agents/{owner}/"
                    if any(
                        path == prefix[:-1] or path.startswith(prefix) for path in git.entries(main)
                    ):
                        raise FileExistsError(f"workspace {owner} already exists")
                    sha = git.commit(
                        main,
                        files,
                        prefixes=(prefix,),
                        owner=owner,
                        message=f"Join workspace {owner}\n",
                    )
                    attempted = sha
                    if git.push(sha, "main", main):
                        return sha
            raise workspace_git.GitError("workspace join push failed after bounded retries")

        sha = await self._run(work)
        await self._observe_main(sha)
        return sha
