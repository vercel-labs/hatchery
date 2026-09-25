"""Local and GitHub review of proposals; only GitHub's HTTP service is replaced."""

import json
import os
import pathlib
import subprocess
import typing
import urllib.parse
import uuid

import httpx
import pytest

from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git
from hatchery.workspace import local as workspace_local
from hatchery.workspace import repo as workspace_repo
from hatchery.workspace import review as workspace_review


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


async def propose(
    repo: workspace_repo.WorkspaceRepo,
    owner: str,
    changes: dict[str, workspace_files.File | None],
    *,
    section: str,
    process_id: str,
    summary: str,
) -> str:
    """Publish a section delta the way a thread does: checkpoint, then consolidate."""
    branch = repo.thread_branch(owner, process_id)
    files = await repo.materialize(owner, branch)
    for path, value in changes.items():
        if value is None:
            files.pop(path, None)
        else:
            files[path] = value
    head = await repo.checkpoint(
        owner, branch, files, process_id=process_id, operation_id=uuid.uuid4().hex
    )
    outcome = await repo.consolidate(
        owner, branch, section, head=head, process_id=process_id, summary=summary
    )
    assert outcome.proposal and not outcome.conflicts, outcome
    return outcome.proposal


@pytest.fixture
async def workspace(tmp_path: pathlib.Path) -> tuple[workspace_repo.WorkspaceRepo, pathlib.Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_text("Shared workspace\n")
    for owner in ("alice", "bob"):
        workspace = root / "agents" / owner
        workspace.mkdir(parents=True)
        (workspace / "AGENTS.md").write_text(f"I am {owner}.\n")
    (root / "agents/alice/old.md").write_text("Obsolete\n")
    (root / "agents/bob/private").mkdir()
    (root / "agents/bob/private/shared.md").write_text("No privacy guarantee\n")
    (root / "wiki").mkdir()
    (root / "wiki/guide.md").write_text("Original wiki\n")
    remote = await workspace_local.initialize_local(root)
    return workspace_repo.WorkspaceRepo(remote), pathlib.Path(
        urllib.parse.unquote(urllib.parse.urlsplit(remote).path)
    )


async def test_local_review_revalidates_full_proposal_diff_before_merge(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    main, _ = await repo.read_main("alice")
    branch = await propose(
        repo,
        "alice",
        {"self/note.md": workspace_files.File(b"Good delta")},
        section="workspace",
        process_id="p",
        summary="Good summary",
    )
    message = git(remote, "show", "-s", "--format=%B", branch)
    git(remote, "read-tree", branch)
    malicious = git(remote, "hash-object", "-w", "--stdin", data=b"Cross-workspace edit")
    git(
        remote,
        "update-index",
        "--add",
        "--cacheinfo",
        "100644",
        malicious,
        "agents/bob/AGENTS.md",
    )
    tree = git(remote, "write-tree")
    bad = git(remote, "commit-tree", tree, "-p", main, data=message.encode())
    git(remote, "update-ref", f"refs/heads/{branch}", bad)
    with pytest.raises(ValueError, match="escaped its declared scope"):
        await workspace_review.LocalReview(repo).merge(branch)
    assert git(remote, "rev-parse", "main") == main
    assert git(remote, "show", "main:agents/bob/AGENTS.md") == "I am bob."


class GitHubWorkspace(workspace_repo.WorkspaceRepo):
    """Use real local proposal Git state, with only GitHub's HTTP service replaced."""

    def __init__(self, local: workspace_repo.WorkspaceRepo):
        super().__init__("https://github.com/acme/workspace.git", token="worker-only-token")
        self.local = local

    async def inspect_proposal(self, branch: str) -> workspace_repo.Proposal:
        return await self.local.inspect_proposal(branch)

    async def delete_proposal(self, branch: str, *, upstream: str = "main") -> bool:
        return await self.local.delete_proposal(branch, upstream=upstream)


@pytest.fixture
async def proposal(tmp_path: pathlib.Path) -> tuple[GitHubWorkspace, str, dict[str, typing.Any]]:
    local = workspace_repo.WorkspaceRepo(await workspace_local.initialize_local(tmp_path))
    await local.join("alice", {"AGENTS.md": workspace_files.File(b"Alice")})
    thread = local.thread_branch("alice", "process-7")
    view = await local.materialize("alice", thread)
    head = await local.checkpoint(
        "alice",
        thread,
        {**view, "wiki/design.md": workspace_files.File(b"Shared design\n")},
        process_id="process-7",
        operation_id="checkpoint",
    )
    outcome = await local.consolidate(
        "alice", thread, "wiki", head=head, process_id="process-7", summary="Curate design"
    )
    branch = outcome.proposal
    inspected = await local.inspect_proposal(branch)
    return (
        GitHubWorkspace(local),
        branch,
        {
            "number": 17,
            "state": "open",
            "merged": False,
            "merged_at": None,
            "head": {
                "ref": branch,
                "sha": inspected.sha,
                "repo": {"full_name": "acme/workspace"},
            },
            "base": {"ref": "main"},
        },
    )


async def test_github_review_opens_one_audited_pr_without_merging(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]],
) -> None:
    repo, branch, pr = proposal
    state: list[dict[str, typing.Any]] = []
    posts: list[dict[str, typing.Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer worker-only-token"
        assert request.url.host == "api.github.com"
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            assert request.url.params["head"] == f"acme:{branch}"
            assert request.url.params["base"] == "main"
            return httpx.Response(200, json=state)
        if request.method == "GET" and request.url.path.endswith("/pulls/17"):
            return httpx.Response(200, json=pr)
        if request.method == "POST":
            posts.append(json.loads(request.content))
            state.append(pr)
            return httpx.Response(201, json=pr)
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        forge = workspace_review.GitHubReview(repo, client=client)
        result = await forge.submit(
            branch=branch,
            summary="Changed retry text",
            owner="alice",
            section="wiki",
            policy="review",
        )
        replay = await forge.submit(
            branch=branch, summary="Another retry", owner="alice", section="wiki", policy="review"
        )

    assert result == replay
    assert result.url == "https://github.com/acme/workspace/pull/17"
    assert not result.merged
    assert result.branch == branch
    assert len(posts) == 1
    assert posts[0]["title"] == "Curate design"
    assert posts[0]["head"] == branch
    assert posts[0]["base"] == "main"
    assert "worker-only-token" not in json.dumps(posts)


async def test_github_unknown_create_outcome_is_deduplicated_by_head(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]],
) -> None:
    repo, branch, pr = proposal
    created = False
    posts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal created, posts
        if request.method == "POST":
            posts += 1
            created = True
            raise httpx.ReadTimeout("response lost after creation", request=request)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[pr] if created else [])
        return httpx.Response(200, json=pr)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await workspace_review.GitHubReview(repo, client=client).submit(
            branch=branch, summary="Design", owner="alice", section="wiki", policy="review"
        )
    assert result.url.endswith("/pull/17")
    assert not result.merged
    assert posts == 1


async def test_github_auto_merge_pins_verified_head_and_recovers_lost_merge_response(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]],
) -> None:
    repo, branch, pr = proposal
    merge_requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            merge_requests.append(json.loads(request.content))
            if not pr["merged"]:
                pr["merged"] = True
                raise httpx.ReadTimeout("merged but response lost", request=request)
            return httpx.Response(405, json={"message": "Already merged"})
        return httpx.Response(200, json=[pr] if request.url.path.endswith("/pulls") else pr)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        forge = workspace_review.GitHubReview(repo, client=client)
        result = await forge.submit(
            branch=branch, summary="Design", owner="alice", section="wiki", policy="auto"
        )
        replay = await forge.submit(
            branch=branch, summary="Design", owner="alice", section="wiki", policy="auto"
        )
    assert result.merged and replay.merged
    assert merge_requests == [
        {"sha": pr["head"]["sha"], "merge_method": "merge"},
        {"sha": pr["head"]["sha"], "merge_method": "merge"},
    ]


@pytest.mark.parametrize(
    "state", ["closed", "changed-head"], ids=["closed-without-merge", "unverified-head"]
)
async def test_github_never_reopens_closed_pr_or_merges_changed_head(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]], state: str
) -> None:
    repo, branch, pr = proposal
    if state == "closed":
        pr["state"] = "closed"
    else:
        pr["head"]["sha"] = "f" * 40

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=[pr] if request.url.path.endswith("/pulls") else pr)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(workspace_review.ReviewRequired):
            await workspace_review.GitHubReview(repo, client=client).submit(
                branch=branch, summary="Design", owner="alice", section="wiki", policy="auto"
            )


async def test_github_transient_failures_are_bounded_without_leaking_token(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]],
) -> None:
    repo, branch, _ = proposal
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="worker-only-token must not appear in errors")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(workspace_git.GitError, match="HTTP 503") as error:
            await workspace_review.GitHubReview(repo, client=client).submit(
                branch=branch, summary="Design", owner="alice", section="wiki", policy="review"
            )
    assert attempts == 4
    assert "worker-only-token" not in str(error.value)


async def test_github_review_gate_fails_honestly_and_keeps_proposal(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]],
) -> None:
    repo, branch, pr = proposal

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(405, json={"message": "Approving review required"})
        return httpx.Response(200, json=[pr] if request.url.path.endswith("/pulls") else pr)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(workspace_review.ReviewRequired, match="HTTP 405.*pull/17"):
            await workspace_review.GitHubReview(repo, client=client).submit(
                branch=branch, summary="Design", owner="alice", section="wiki", policy="auto"
            )
    assert not pr["merged"]
    assert (await repo.local.inspect_proposal(branch)).sha == pr["head"]["sha"]


async def test_github_withdraw_closes_the_open_pr_and_deletes_its_branch(
    proposal: tuple[GitHubWorkspace, str, dict[str, typing.Any]],
) -> None:
    repo, branch, pr = proposal
    patches: list[dict[str, typing.Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[pr])
        if request.method == "PATCH" and request.url.path.endswith("/pulls/17"):
            patches.append(json.loads(request.content))
            return httpx.Response(200, json={**pr, "state": "closed"})
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await workspace_review.GitHubReview(repo, client=client).withdraw(branch)

    assert patches == [{"state": "closed"}]
    with pytest.raises(workspace_git.GitError, match="missing workspace branch"):
        await repo.local.inspect_proposal(branch)
