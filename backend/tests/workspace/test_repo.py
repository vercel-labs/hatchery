"""Thread branches, checkpoints, consolidation, refresh, and accept against real local Git."""

import asyncio
import os
import pathlib
import subprocess
import threading
import typing
import urllib.parse
import uuid

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


def agents_md(text: str) -> dict[str, workspace_files.File]:
    return {"AGENTS.md": workspace_files.File(text.encode())}


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


async def test_materialization_keeps_thread_self_and_wiki_but_refreshes_collective(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "thread/42; not a command")
    first = await repo.materialize("alice", branch)
    assert first == {
        "self/AGENTS.md": workspace_files.File(b"I am alice.\n"),
        "self/old.md": workspace_files.File(b"Obsolete\n"),
        "wiki/guide.md": workspace_files.File(b"Original wiki\n"),
        "collective/bob/AGENTS.md": workspace_files.File(b"I am bob.\n"),
        "collective/bob/private/shared.md": workspace_files.File(b"No privacy guarantee\n"),
    }
    await repo.join("carol", agents_md("I am Carol.\n"))
    second = await repo.materialize("alice", branch)
    assert second["collective/carol/AGENTS.md"].content == b"I am Carol.\n"
    assert second["self/AGENTS.md"] == first["self/AGENTS.md"]
    assert second["wiki/guide.md"] == first["wiki/guide.md"]
    assert git(remote, "rev-parse", branch) != git(remote, "rev-parse", "main")
    assert await repo.list_agents() == ["alice", "bob", "carol"]


async def test_checkpoint_replaces_exact_trees_without_staging_collective_and_replay_is_stale_safe(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "process-7")
    initial_main = git(remote, "rev-parse", "main")
    first = await repo.checkpoint(
        "alice",
        branch,
        {
            "self/AGENTS.md": workspace_files.File(b"Updated instructions\n"),
            "self/run.sh": workspace_files.File(b"exit 7\n", True),
            "collective/bob/AGENTS.md": workspace_files.File(b"Attempted cross-owner edit\n"),
        },
        process_id="process-7",
        operation_id="save-1",
    )
    assert git(remote, "show", f"{first}:agents/bob/AGENTS.md") == "I am bob."
    assert git(remote, "diff", "--name-status", initial_main, first).splitlines() == [
        "M\tagents/alice/AGENTS.md",
        "D\tagents/alice/old.md",
        "A\tagents/alice/run.sh",
        "D\twiki/guide.md",
    ]
    assert git(remote, "ls-tree", first, "agents/alice/run.sh").startswith("100755 blob ")
    assert git(remote, "show", "-s", "--format=%an", first) == "hatchery/alice"
    assert "Rotor-Process: process-7" in git(remote, "show", "-s", "--format=%B", first)

    second = await repo.checkpoint(
        "alice",
        branch,
        {"self/AGENTS.md": workspace_files.File(b"Newest instructions\n")},
        process_id="process-7",
        operation_id="save-2",
    )
    replay = await repo.checkpoint(
        "alice",
        branch,
        {"self/AGENTS.md": workspace_files.File(b"Stale retry content\n")},
        process_id="process-7",
        operation_id="save-1",
    )
    assert replay == first
    assert git(remote, "rev-parse", branch) == second
    assert git(remote, "rev-parse", "main") == initial_main
    assert await repo.materialize("alice", branch) == {
        "self/AGENTS.md": workspace_files.File(b"Newest instructions\n"),
        "collective/bob/AGENTS.md": workspace_files.File(b"I am bob.\n"),
        "collective/bob/private/shared.md": workspace_files.File(b"No privacy guarantee\n"),
    }


async def test_checkpoint_omits_python_cache_artifacts(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, _ = workspace
    branch = repo.thread_branch("alice", "python-cache")
    files = await repo.materialize("alice", branch)
    files["self/__pycache__/worker.cpython-312.pyc"] = workspace_files.File(b"bytecode")
    files["self/worker.pyo"] = workspace_files.File(b"optimized")

    await repo.checkpoint(
        "alice", branch, files, process_id="python-cache", operation_id="checkpoint"
    )

    refreshed = await repo.materialize("alice", branch)
    assert "self/__pycache__/worker.cpython-312.pyc" not in refreshed
    assert "self/worker.pyo" not in refreshed


@pytest.mark.parametrize(
    "path",
    [
        "self//AGENTS.md",
        "/self/AGENTS.md",
        "self/./AGENTS.md",
        "self/.git/config",
        "wiki/.GiT/hooks/pre-commit",
        "self/git~1/config",
        "self/a\\b",
        "self/a\nRotor-Operation: forged",
        "self/:(top)agents/bob/AGENTS.md",
        "agents/bob/AGENTS.md",
        "self/.git./config",
        "self/\u200c.git/config",
        "self",
        "self/../bob/AGENTS.md",
    ],
)
async def test_checkpoint_rejects_injection_paths_before_mutation(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], path: str
) -> None:
    repo, remote = workspace
    before = git(remote, "show-ref")
    with pytest.raises(ValueError):
        await repo.checkpoint(
            "alice",
            repo.thread_branch("alice", "p"),
            {path: workspace_files.File(b"malicious")},
            process_id="p",
            operation_id="attack",
        )
    assert git(remote, "show-ref") == before


async def test_thread_owner_and_process_trailer_cannot_inject_refs_or_audit_fields(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    before = git(remote, "show-ref")
    with pytest.raises(ValueError, match="thread branch"):
        await repo.materialize("alice", repo.thread_branch("bob", "p"))
    with pytest.raises(ValueError, match="single-line"):
        await repo.checkpoint(
            "alice",
            repo.thread_branch("alice", "p"),
            {},
            process_id="p\nRotor-Operation: forged",
            operation_id="x",
        )
    with pytest.raises(ValueError, match="thread branch"):
        await repo.materialize(
            "alice", repo.thread_branch("alice", "child"), upstream="refs/heads/main"
        )
    with pytest.raises(ValueError, match="thread branch"):
        await repo.materialize(
            "alice",
            repo.thread_branch("alice", "child"),
            upstream=repo.thread_branch("bob", "foreign-parent"),
        )
    assert git(remote, "show-ref") == before


@pytest.mark.parametrize("mode", ["120000", "160000"], ids=["symlink", "gitlink"])
async def test_materialization_rejects_nonregular_git_entries(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], mode: str
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "untrusted")
    main = git(remote, "rev-parse", "main")
    oid = (
        git(remote, "hash-object", "-w", "--stdin", data=b"/etc/passwd")
        if mode == "120000"
        else main
    )
    subtree = git(
        remote,
        "mktree",
        data=f"{mode} {'blob' if mode == '120000' else 'commit'} {oid}\tescape\n".encode(),
    )
    tree = git(remote, "mktree", data=f"040000 tree {subtree}\twiki\n".encode())
    commit = git(remote, "commit-tree", tree, "-p", main, data=b"Untrusted repository entry\n")
    git(remote, "update-ref", f"refs/heads/{branch}", commit)
    with pytest.raises(ValueError, match="link, special file"):
        await repo.materialize("alice", branch)


async def test_git_environment_index_and_hooks_are_isolated(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, remote = workspace
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "pre-push", "pre-receive", "post-receive"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        hook.chmod(0o755)
    config = tmp_path / "evil.gitconfig"
    config.write_text(f"[core]\n hooksPath = {hooks}\n[alias]\n commit = !false\n")
    index = tmp_path / "unrelated-index"
    index.write_bytes(b"must remain byte-for-byte unchanged")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    monkeypatch.setenv("GIT_INDEX_FILE", str(index))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong.git"))
    git(remote, "config", "core.hooksPath", str(hooks))

    branch = repo.thread_branch("alice", "isolated")
    await repo.checkpoint(
        "alice",
        branch,
        {"self/AGENTS.md": workspace_files.File(b"Safe\n")},
        process_id="isolated",
        operation_id="save",
    )

    assert not marker.exists()
    assert index.read_bytes() == b"must remain byte-for-byte unchanged"
    assert git(remote, "show", f"{branch}:agents/alice/AGENTS.md") == "Safe"


@pytest.mark.parametrize(
    "limit, files",
    [
        (
            "MAX_FILES",
            {"self/one": workspace_files.File(b"a"), "self/two": workspace_files.File(b"b")},
        ),
        ("MAX_FILE_BYTES", {"self/one": workspace_files.File(b"ab")}),
        ("MAX_TREE_BYTES", {"self/one": workspace_files.File(b"ab")}),
    ],
)
async def test_checkpoint_bounds_input_before_creating_refs(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    files: dict[str, workspace_files.File],
) -> None:
    repo, remote = workspace
    monkeypatch.setattr(f"hatchery.workspace.files.{limit}", 1)
    before = git(remote, "show-ref")
    with pytest.raises(ValueError, match="limit"):
        await repo.checkpoint(
            "alice", repo.thread_branch("alice", "p"), files, process_id="p", operation_id="bound"
        )
    assert git(remote, "show-ref") == before


async def test_consolidation_separates_workspace_auto_merge_from_wiki_review(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    thread = repo.thread_branch("alice", "p")
    view = await repo.materialize("alice", thread)
    view.pop("self/old.md")
    head = await repo.checkpoint(
        "alice",
        thread,
        {
            **view,
            "self/AGENTS.md": workspace_files.File(b"Curated\n"),
            "wiki/guide.md": workspace_files.File(b"Reviewed wiki\n"),
        },
        process_id="p",
        operation_id="checkpoint",
    )
    main, _ = await repo.read_main("alice")

    workspace_outcome = await repo.consolidate(
        "alice", thread, "workspace", head=head, process_id="p", summary="Curate identity"
    )
    proposal_branch = workspace_outcome.proposal
    assert proposal_branch.startswith("consolidations/alice/workspace/")
    assert git(remote, "show", "-s", "--format=%P", proposal_branch) == main
    assert git(remote, "diff", "--name-only", main, proposal_branch).splitlines() == [
        "agents/alice/AGENTS.md",
        "agents/alice/old.md",
    ]
    forge = workspace_review.LocalReview(repo)
    result = await forge.submit(
        branch=proposal_branch,
        summary="Curate identity",
        owner="alice",
        section="workspace",
        policy="auto",
    )
    assert result.merged
    assert "Merge workspace: Curate identity" in git(remote, "show", "-s", "--format=%B", "main")
    new_main, merged_view = await repo.read_main("alice")
    assert merged_view["self/AGENTS.md"].content == b"Curated\n"
    assert "self/old.md" not in merged_view
    assert merged_view["wiki/guide.md"].content == b"Original wiki\n", "wiki awaits review"

    wiki_outcome = await repo.consolidate(
        "alice", thread, "wiki", head=head, process_id="p", summary="Improve guide"
    )
    pending = await forge.submit(
        branch=wiki_outcome.proposal,
        summary="Improve guide",
        owner="alice",
        section="wiki",
        policy="review",
    )
    assert not pending.merged
    assert git(remote, "rev-parse", "main") == new_main
    merged = await forge.merge(wiki_outcome.proposal)
    assert git(remote, "show", "main:wiki/guide.md") == "Reviewed wiki"
    assert await forge.merge(wiki_outcome.proposal) == merged


async def test_redelivered_consolidation_reuses_the_proposal_and_a_withdrawn_one_is_deleted(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    thread = repo.thread_branch("alice", "again")
    view = await repo.materialize("alice", thread)
    head = await repo.checkpoint(
        "alice",
        thread,
        {**view, "wiki/draft.md": workspace_files.File(b"Draft\n")},
        process_id="again",
        operation_id="first",
    )
    first = await repo.consolidate(
        "alice", thread, "wiki", head=head, process_id="again", summary="Draft"
    )
    tip = git(remote, "rev-parse", first.proposal)
    again = await repo.consolidate(
        "alice", thread, "wiki", head=head, process_id="again", summary="Draft"
    )
    assert again.proposal == first.proposal
    assert git(remote, "rev-parse", first.proposal) == tip, "an identical proposal is not re-pushed"

    reverted = await repo.checkpoint(
        "alice", thread, view, process_id="again", operation_id="revert"
    )
    withdrawn = await repo.consolidate(
        "alice", thread, "wiki", head=reverted, process_id="again", summary="Revert"
    )
    assert withdrawn.obsolete == first.proposal and not withdrawn.proposal
    assert await repo.delete_proposal(first.proposal)
    assert git(remote, "for-each-ref", "--format=%(refname)", f"refs/heads/{first.proposal}") == ""
    assert not await repo.delete_proposal(first.proposal), "already gone"


async def test_delete_proposal_leaves_a_merged_branch_alone(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = await propose(
        repo,
        "alice",
        {"wiki/keep.md": workspace_files.File(b"Merged history\n")},
        section="wiki",
        process_id="keep",
        summary="Keep",
    )
    sha = git(remote, "rev-parse", branch)
    await workspace_review.LocalReview(repo).merge(branch)

    assert not await repo.delete_proposal(branch)
    assert git(remote, "rev-parse", branch) == sha


async def test_git_merges_disjoint_thread_and_main_edits_without_conflicts(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "merge-disjoint")
    view = await repo.materialize("alice", branch)
    head = await repo.checkpoint(
        "alice",
        branch,
        {
            **view,
            "wiki/guide.md": workspace_files.File(
                b"Original wiki\nThread adds a final paragraph.\n"
            ),
        },
        process_id="merge-disjoint",
        operation_id="checkpoint",
    )
    teammate = await propose(
        repo,
        "alice",
        {
            "wiki/guide.md": workspace_files.File(
                b"Main adds an opening paragraph.\nOriginal wiki\n"
            )
        },
        section="wiki",
        process_id="teammate",
        summary="Add guide introduction",
    )
    await workspace_review.LocalReview(repo).merge(teammate)

    outcome = await repo.consolidate(
        "alice", branch, "wiki", head=head, process_id="merge-disjoint", summary="Extend"
    )
    assert outcome.conflicts == () and outcome.proposal
    await workspace_review.LocalReview(repo).merge(outcome.proposal)
    assert git(remote, "show", "main:wiki/guide.md") == (
        "Main adds an opening paragraph.\nOriginal wiki\nThread adds a final paragraph."
    )


async def test_conflicting_section_returns_to_the_thread_as_markers_it_repairs(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "merge-conflict")
    view = await repo.materialize("alice", branch)
    head = await repo.checkpoint(
        "alice",
        branch,
        {
            **view,
            "wiki/guide.md": workspace_files.File(b"Thread guide\n"),
            "wiki/only.md": workspace_files.File(b"No conflict\n"),
        },
        process_id="merge-conflict",
        operation_id="checkpoint",
    )
    teammate = await propose(
        repo,
        "alice",
        {"wiki/guide.md": workspace_files.File(b"Main guide\n")},
        section="wiki",
        process_id="teammate",
        summary="Revise the guide on main",
    )
    await workspace_review.LocalReview(repo).merge(teammate)

    outcome = await repo.consolidate(
        "alice", branch, "wiki", head=head, process_id="merge-conflict", summary="Combine"
    )
    assert outcome.conflicts == ("wiki/guide.md",) and not outcome.proposal

    base = await repo.thread_base("alice", branch)
    refreshed = await repo.refresh(
        "alice", branch, head=head, base=base, process_id="merge-conflict"
    )
    assert refreshed.conflicts == ("wiki/guide.md",)
    assert refreshed.updated == (), "conflicts are reported separately"
    assert refreshed.files["wiki/guide.md"].content == (
        b"<<<<<<< main\nMain guide\n=======\nThread guide\n>>>>>>> thread\n"
    )
    assert refreshed.files["wiki/only.md"].content == b"No conflict\n"

    unrepaired = await repo.consolidate(
        "alice", branch, "wiki", head=refreshed.sha, process_id="merge-conflict", summary="x"
    )
    assert unrepaired.conflicts == ("wiki/guide.md",), "markers never reach a proposal"

    repaired = await repo.checkpoint(
        "alice",
        branch,
        {**refreshed.files, "wiki/guide.md": workspace_files.File(b"Combined guide\n")},
        process_id="merge-conflict",
        operation_id="repaired",
    )
    published = await repo.consolidate(
        "alice", branch, "wiki", head=repaired, process_id="merge-conflict", summary="Combine"
    )
    assert published.proposal and not published.conflicts
    await workspace_review.LocalReview(repo).merge(published.proposal)
    assert git(remote, "show", "main:wiki/guide.md") == "Combined guide"
    assert git(remote, "show", "main:wiki/only.md") == "No conflict"


async def test_re_editing_a_published_file_merges_cleanly_even_after_its_branch_is_gone(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "section-base")
    view = await repo.materialize("alice", branch)
    first_head = await repo.checkpoint(
        "alice",
        branch,
        {
            **view,
            "self/note.md": workspace_files.File(b"first\n"),
            "wiki/guide.md": workspace_files.File(b"thread wiki\n"),
        },
        process_id="section-base",
        operation_id="first",
    )
    first = await repo.consolidate(
        "alice", branch, "workspace", head=first_head, process_id="section-base", summary="One"
    )
    await workspace_review.LocalReview(repo).merge(first.proposal)
    git(remote, "update-ref", "-d", f"refs/heads/{first.proposal}")  # as GitHub does after merge

    second_head = await repo.checkpoint(
        "alice",
        branch,
        {
            **view,
            "self/note.md": workspace_files.File(b"second\n"),
            "wiki/guide.md": workspace_files.File(b"thread wiki\n"),
        },
        process_id="section-base",
        operation_id="second",
    )
    second = await repo.consolidate(
        "alice", branch, "workspace", head=second_head, process_id="section-base", summary="Two"
    )
    assert second.conflicts == () and second.proposal
    await workspace_review.LocalReview(repo).merge(second.proposal)
    assert git(remote, "show", "main:agents/alice/note.md") == "second"

    wiki = await repo.consolidate(
        "alice", branch, "wiki", head=second_head, process_id="section-base", summary="Wiki"
    )
    assert wiki.conflicts == () and wiki.proposal, "the wiki section keeps its own base"


async def test_refresh_merges_current_main_into_a_thread_without_publishing_local_work(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "refresh-clean")
    base, view = await repo.read_main("alice")
    head = await repo.checkpoint(
        "alice",
        branch,
        {**view, "self/local.md": workspace_files.File(b"Local thread work\n")},
        process_id="refresh-clean",
        operation_id="checkpoint",
    )
    teammate = await propose(
        repo,
        "alice",
        {"wiki/from-main.md": workspace_files.File(b"Accepted shared update\n")},
        section="wiki",
        process_id="teammate",
        summary="Add shared update",
    )
    main = await workspace_review.LocalReview(repo).merge(teammate)

    refreshed = await repo.refresh(
        "alice", branch, head=head, base=base, process_id="refresh-clean"
    )

    assert refreshed.main == main and refreshed.conflicts == ()
    assert refreshed.updated == (), "the thread never touched the upstream-only file"
    assert refreshed.changed == ("wiki/from-main.md",)
    assert refreshed.files["self/local.md"].text == "Local thread work\n"
    assert refreshed.files["wiki/from-main.md"].text == "Accepted shared update\n"
    assert git(remote, "merge-base", "--is-ancestor", main, refreshed.sha) == ""
    _, current = await repo.read_main("alice")
    assert "self/local.md" not in current

    revised = await propose(
        repo,
        "alice",
        {"wiki/from-main.md": workspace_files.File(b"Revised upstream update\n")},
        section="wiki",
        process_id="teammate",
        summary="Revise shared update",
    )
    await workspace_review.LocalReview(repo).merge(revised)
    refreshed_again = await repo.refresh(
        "alice",
        branch,
        head=refreshed.sha,
        base=base,
        process_id="refresh-clean",
    )
    assert refreshed_again.files["wiki/from-main.md"].text == "Revised upstream update\n"
    assert refreshed_again.updated == (), "a prior refresh does not make a file thread-touched"
    assert refreshed_again.changed == ("wiki/from-main.md",)


async def test_section_merge_does_not_follow_renames_across_workspace_and_wiki(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "cross-section-rename")
    moved = await repo.materialize("alice", branch)
    original = moved.pop("self/old.md")
    moved["wiki/old.md"] = original
    head = await repo.checkpoint(
        "alice",
        branch,
        moved,
        process_id="cross-section-rename",
        operation_id="checkpoint",
    )
    teammate = await propose(
        repo,
        "alice",
        {"self/old.md": workspace_files.File(b"New main workspace content\n")},
        section="workspace",
        process_id="teammate",
        summary="Edit workspace source",
    )
    await workspace_review.LocalReview(repo).merge(teammate)

    outcome = await repo.consolidate(
        "alice", branch, "wiki", head=head, process_id="cross-section-rename", summary="Move"
    )
    assert outcome.conflicts == () and outcome.proposal
    await workspace_review.LocalReview(repo).merge(outcome.proposal)

    assert git(remote, "show", "main:wiki/old.md") == original.text.strip()
    assert git(remote, "show", "main:agents/alice/old.md") == "New main workspace content"


async def test_local_merge_preserves_unrelated_main_changes_and_detects_overlap(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    main, _ = await repo.read_main("alice")
    first = await propose(
        repo,
        "alice",
        {"self/AGENTS.md": workspace_files.File(b"First\n")},
        section="workspace",
        process_id="first-thread",
        summary="First",
    )
    conflict = await propose(
        repo,
        "alice",
        {"self/AGENTS.md": workspace_files.File(b"Conflicting\n")},
        section="workspace",
        process_id="second-thread",
        summary="Conflict",
    )
    await repo.join("carol", agents_md("Preserve this unrelated main update\n"))
    forge = workspace_review.LocalReview(repo)
    merged = await forge.merge(first)
    assert (
        git(remote, "show", "main:agents/carol/AGENTS.md") == "Preserve this unrelated main update"
    )
    with pytest.raises(workspace_git.MergeConflict, match="agents/alice/AGENTS.md"):
        await forge.merge(conflict)
    assert git(remote, "rev-parse", "main") == merged
    assert git(remote, "show", "main:agents/alice/AGENTS.md") == "First"


async def test_join_refuses_existing_workspace_without_changes(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    before = git(remote, "rev-parse", "main")
    with pytest.raises(FileExistsError):
        await repo.join("alice", agents_md("Replacement"))
    assert git(remote, "rev-parse", "main") == before


async def test_simultaneous_joins_retry_compare_and_swap_without_lost_workspaces(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = workspace
    original = subprocess.run
    barrier = threading.Barrier(2)
    claims: set[int] = set()
    lock = threading.Lock()

    def interleave(args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        if "push" in args and any(str(arg).endswith(":refs/heads/main") for arg in args):
            identity = threading.get_ident()
            with lock:
                first = identity not in claims
                claims.add(identity)
            if first:
                barrier.wait(timeout=10)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", interleave)
    await asyncio.gather(
        repo.join("carol", agents_md("Carol")), repo.join("dave", agents_md("Dave"))
    )
    assert await repo.list_agents() == ["alice", "bob", "carol", "dave"]


async def test_uncertain_checkpoint_push_recovery_returns_original_commit(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "p")
    await repo.materialize("alice", branch)
    original = subprocess.run
    lost = False

    def lose_push_response(args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        nonlocal lost
        result = original(args, **kwargs)
        if "push" in args and not lost:
            lost = True
            return subprocess.CompletedProcess(
                args, 1, b"", b"connection lost after successful push"
            )
        return result

    monkeypatch.setattr(subprocess, "run", lose_push_response)
    sha = await repo.checkpoint(
        "alice",
        branch,
        {"self/AGENTS.md": workspace_files.File(b"Once\n")},
        process_id="p",
        operation_id="uncertain",
    )
    assert lost
    assert git(remote, "rev-parse", branch) == sha
    assert git(remote, "rev-list", "--count", f"main..{branch}") == "1"


async def test_push_failures_are_bounded_and_leave_main_unchanged(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = workspace
    before = git(remote, "rev-parse", "main")
    original = subprocess.run
    attempts = 0

    def reject_push(args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        nonlocal attempts
        if "push" in args:
            attempts += 1
            return subprocess.CompletedProcess(args, 1, b"", b"permission denied")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_push)
    with pytest.raises(workspace_git.GitError, match="bounded retries"):
        await repo.join("carol", agents_md("Carol"))
    assert attempts == 4
    assert git(remote, "rev-parse", "main") == before


async def test_local_merge_rejects_directory_replacement_that_would_erase_new_main_files(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    main, _ = await repo.read_main("alice")
    replace = await propose(
        repo,
        "alice",
        {"self/notes": workspace_files.File(b"Single file\n")},
        section="workspace",
        process_id="file-thread",
        summary="Create notes file",
    )
    directory = await propose(
        repo,
        "alice",
        {"self/notes/new.md": workspace_files.File(b"Preserve this\n")},
        section="workspace",
        process_id="directory-thread",
        summary="Create notes tree",
    )
    forge = workspace_review.LocalReview(repo)
    merged = await forge.merge(directory)
    with pytest.raises(workspace_git.MergeConflict, match="file/directory structure"):
        await forge.merge(replace)
    assert git(remote, "rev-parse", "main") == merged
    assert git(remote, "show", "main:agents/alice/notes/new.md") == "Preserve this"


async def test_reused_git_session_is_bounded_by_operations_and_size(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = workspace
    object_dirs: list[pathlib.Path] = []
    original_init = workspace_git.Git.__init__

    def recording_init(self: workspace_git.Git, remote: str, token: str | None = None) -> None:
        original_init(self, remote, token)
        object_dirs.append(self.path)

    monkeypatch.setattr(workspace_git.Git, "__init__", recording_init)
    monkeypatch.setattr("hatchery.workspace.repo.GIT_SESSION_OPERATIONS", 3)

    await repo.list_agents()
    await repo.list_agents()
    assert len(object_dirs) == 1, "sequential operations share one isolated database"
    assert object_dirs[0].exists()

    await repo.list_agents()
    assert len(object_dirs) == 1
    assert not object_dirs[0].exists(), "the third operation exhausted and removed it"

    await repo.list_agents()
    assert len(object_dirs) == 2, "the next operation opened a fresh database"
    assert object_dirs[1].exists()

    monkeypatch.setattr("hatchery.workspace.repo.GIT_SESSION_BYTES", 1)
    await repo.list_agents()
    assert not object_dirs[1].exists(), "an oversized database is discarded when returned"

    repo.close()
    assert not any(path.exists() for path in object_dirs)


async def test_concurrent_same_operation_checkpoints_commit_only_once(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, remote = workspace
    branch = repo.thread_branch("alice", "p")
    await repo.materialize("alice", branch)
    barrier = threading.Barrier(2)
    original = subprocess.run

    def simultaneous_push(args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        if "push" in args:
            barrier.wait(timeout=10)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", simultaneous_push)
    first, second = await asyncio.gather(
        repo.checkpoint(
            "alice",
            branch,
            {"self/note": workspace_files.File(b"First result")},
            process_id="p",
            operation_id="same",
        ),
        repo.checkpoint(
            "alice",
            branch,
            {"self/note": workspace_files.File(b"Retry result")},
            process_id="p",
            operation_id="same",
        ),
    )
    assert first == second
    assert git(remote, "rev-list", "--count", f"main..{branch}") == "1"


async def test_join_recovers_a_lost_successful_push_response(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, remote = workspace
    original = subprocess.run
    pushes = 0

    def lose_response(args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        nonlocal pushes
        result = original(args, **kwargs)
        if "push" in args:
            pushes += 1
            return subprocess.CompletedProcess(args, 1, b"", b"lost response")
        return result

    monkeypatch.setattr(subprocess, "run", lose_response)
    sha = await repo.join("carol", agents_md("Carol"))
    assert git(remote, "rev-parse", "main") == sha
    assert pushes == 1
    assert await repo.list_agents() == ["alice", "bob", "carol"]


async def test_workspace_rejects_a_working_repository_as_its_file_remote(
    tmp_path: pathlib.Path,
) -> None:
    await workspace_local.initialize_local(tmp_path)
    with pytest.raises(ValueError, match="bare Git repository"):
        await workspace_repo.WorkspaceRepo(tmp_path.as_uri()).list_agents()


async def test_thread_base_remains_the_fork_commit_as_main_and_the_branch_advance(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, _ = workspace
    main, original = await repo.read_main("alice")
    branch = repo.thread_branch("alice", "base-test-process")
    assert await repo.thread_base("alice", branch) == main

    first = {**original, "self/first.md": workspace_files.File(b"first\n")}
    await repo.checkpoint(
        "alice", branch, first, process_id="base-test-process", operation_id="first"
    )
    second = {**first, "self/second.md": workspace_files.File(b"second\n")}
    await repo.checkpoint(
        "alice", branch, second, process_id="base-test-process", operation_id="second"
    )
    await repo.join("charlie", agents_md("I am Charlie.\n"))

    assert await repo.thread_base("alice", branch) == main
    baseline = await repo.read_at("alice", main)
    assert "self/first.md" not in baseline and "self/second.md" not in baseline
    assert "collective/charlie/AGENTS.md" not in baseline


async def test_child_materializes_from_the_explicit_parent_checkpoint(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent_process = "parent-explicit-fork"
    parent = repo.thread_branch("alice", parent_process)
    parent_files = await repo.materialize("alice", parent)
    checkpoint = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Plan at delegation\n")},
        process_id=parent_process,
        operation_id="delegate-checkpoint",
    )
    await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Plan after delegation\n")},
        process_id=parent_process,
        operation_id="later-parent-work",
    )

    child = repo.thread_branch("alice", "child-explicit-fork")
    child_files = await repo.materialize("alice", child, upstream=parent, upstream_sha=checkpoint)

    assert child_files["self/plan.md"].text == "Plan at delegation\n"
    assert child_files["collective/bob/AGENTS.md"].text == "I am bob.\n"
    assert git(remote, "rev-parse", child) == checkpoint
    assert await repo.thread_base("alice", child, upstream=parent) == checkpoint


async def test_child_refresh_merges_later_parent_work(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent_process = "parent-later-work"
    parent = repo.thread_branch("alice", parent_process)
    parent_files = await repo.materialize("alice", parent)
    fork = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Initial plan\n")},
        process_id=parent_process,
        operation_id="initial-plan",
    )
    child_process = "child-pulls-parent"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent, upstream_sha=fork)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/child.md": workspace_files.File(b"Local child work\n")},
        process_id=child_process,
        operation_id="child-work",
    )
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Revised parent plan\n")},
        process_id=parent_process,
        operation_id="revised-plan",
    )

    refreshed = await repo.refresh(
        "alice",
        child,
        head=child_head,
        base=fork,
        process_id=child_process,
        upstream=parent,
    )

    assert refreshed.upstream == parent_head
    assert refreshed.main == git(remote, "rev-parse", "main")
    assert refreshed.files["self/plan.md"].text == "Revised parent plan\n"
    assert refreshed.files["self/child.md"].text == "Local child work\n"
    assert refreshed.updated == ()
    assert refreshed.changed == ("self/plan.md",)
    assert git(remote, "show", "-s", "--format=%P", refreshed.sha).split() == [
        child_head,
        parent_head,
    ]
    rematerialized = await repo.materialize("alice", child, upstream=parent, upstream_sha=fork)
    assert rematerialized["self/child.md"].text == "Local child work\n"


async def test_child_consolidation_proposal_is_based_on_the_parent(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent_process = "proposal-parent"
    parent = repo.thread_branch("alice", parent_process)
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Parent plan\n")},
        process_id=parent_process,
        operation_id="plan",
    )
    child_process = "proposal-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {
            **child_files,
            "self/memories/child-findings.md": workspace_files.File(b"Child findings\n"),
        },
        process_id=child_process,
        operation_id="findings",
    )

    outcome = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_head,
        process_id=child_process,
        summary="Record child findings",
        upstream=parent,
    )
    proposal = await repo.inspect_proposal(outcome.proposal)

    assert outcome.upstream == parent_head and outcome.conflicts == ()
    assert proposal.base == parent_head and proposal.process == child_process
    assert proposal.thread == child_head
    assert git(remote, "show", "-s", "--format=%P", outcome.proposal) == parent_head
    assert await repo.merged_proposals([outcome.proposal], upstream=parent) == {
        outcome.proposal: False
    }


async def test_accept_merges_the_child_proposal_as_the_parent_second_parent(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    main = git(remote, "rev-parse", "main")
    parent_process = "accept-parent"
    parent = repo.thread_branch("alice", parent_process)
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Parent plan\n")},
        process_id=parent_process,
        operation_id="plan",
    )
    child_process = "accept-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {
            **child_files,
            "self/memories/accepted-result.md": workspace_files.File(b"Accepted result\n"),
        },
        process_id=child_process,
        operation_id="result",
    )
    proposed = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_head,
        process_id=child_process,
        summary="Add delegated result",
        upstream=parent,
    )
    proposal_sha = git(remote, "rev-parse", proposed.proposal)

    accepted = await repo.accept(
        "alice",
        parent,
        proposed.proposal,
        head=parent_head,
        proposal_sha=proposal_sha,
        child_process_id=child_process,
        task_id="task-4",
    )

    assert git(remote, "rev-parse", parent) == accepted.sha
    assert git(remote, "show", "-s", "--format=%P", accepted.sha).split() == [
        parent_head,
        proposal_sha,
    ]
    assert accepted.upstream == proposal_sha and accepted.main == main
    assert accepted.changed == ("self/memories/accepted-result.md",)
    assert accepted.files["self/memories/accepted-result.md"].text == "Accepted result\n"
    assert git(remote, "rev-parse", "main") == main
    assert await repo.merged_proposals([proposed.proposal], upstream=parent) == {
        proposed.proposal: True
    }


async def test_accepted_child_can_reedit_the_same_file_without_conflict(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", "reedit-parent")
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Parent plan\n")},
        process_id="reedit-parent",
        operation_id="plan",
    )
    child_process = "reedit-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    first_child_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/memories/reedit-result.md": workspace_files.File(b"First version\n")},
        process_id=child_process,
        operation_id="first-version",
    )
    first = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=first_child_head,
        process_id=child_process,
        summary="Add first version",
        upstream=parent,
    )
    first_sha = git(remote, "rev-parse", first.proposal)
    accepted = await repo.accept(
        "alice",
        parent,
        first.proposal,
        head=parent_head,
        proposal_sha=first_sha,
        child_process_id=child_process,
        task_id="reedit",
    )

    second_child_head = await repo.checkpoint(
        "alice",
        child,
        {
            **child_files,
            "self/memories/reedit-result.md": workspace_files.File(b"Second version\n"),
        },
        process_id=child_process,
        operation_id="second-version",
    )
    second = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=second_child_head,
        process_id=child_process,
        summary="Revise accepted result",
        upstream=parent,
    )

    assert second.conflicts == () and second.proposal
    assert (await repo.inspect_proposal(second.proposal)).base == accepted.sha
    assert git(remote, "show", "-s", "--format=%P", second.proposal) == accepted.sha
    assert git(remote, "show", f"{second.proposal}:agents/alice/memories/reedit-result.md") == (
        "Second version"
    )


async def test_conflicting_accept_leaves_parent_unchanged_and_child_refresh_gets_markers(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent_process = "conflict-parent"
    parent = repo.thread_branch("alice", parent_process)
    parent_files = await repo.materialize("alice", parent)
    fork = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/shared.md": workspace_files.File(b"Shared base\n")},
        process_id=parent_process,
        operation_id="shared-base",
    )
    child_process = "conflict-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/shared.md": workspace_files.File(b"Child edit\n")},
        process_id=child_process,
        operation_id="child-edit",
    )
    proposed = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_head,
        process_id=child_process,
        summary="Edit shared memory",
        upstream=parent,
    )
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/shared.md": workspace_files.File(b"Parent edit\n")},
        process_id=parent_process,
        operation_id="parent-edit",
    )

    with pytest.raises(workspace_git.MergeConflict, match="self/shared.md"):
        await repo.accept(
            "alice",
            parent,
            proposed.proposal,
            head=parent_head,
            proposal_sha=git(remote, "rev-parse", proposed.proposal),
            child_process_id=child_process,
            task_id="conflict-task",
        )
    assert git(remote, "rev-parse", parent) == parent_head
    assert git(remote, "show", f"{parent}:agents/alice/shared.md") == "Parent edit"

    refreshed = await repo.refresh(
        "alice",
        child,
        head=child_head,
        base=fork,
        process_id=child_process,
        upstream=parent,
    )

    assert refreshed.upstream == parent_head
    assert refreshed.conflicts == ("self/shared.md",)
    assert refreshed.files["self/shared.md"].content == (
        b"<<<<<<< parent\nParent edit\n=======\nChild edit\n>>>>>>> thread\n"
    )
    unresolved = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=refreshed.sha,
        process_id=child_process,
        summary="Still conflicted",
        upstream=parent,
    )
    assert unresolved.conflicts == ("self/shared.md",) and not unresolved.proposal


async def test_explicit_fork_rejects_its_upstream_and_a_preexisting_wrong_child_ref(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", "exact-fork-parent")
    parent_files = await repo.materialize("alice", parent)
    fork = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/parent.md": workspace_files.File(b"Parent-only history\n")},
        process_id="exact-fork-parent",
        operation_id="parent-work",
    )
    later_parent = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/parent.md": workspace_files.File(b"Later parent history\n")},
        process_id="exact-fork-parent",
        operation_id="later-parent-work",
    )
    child = repo.thread_branch("alice", "exact-fork-child")
    git(remote, "update-ref", f"refs/heads/{child}", later_parent)

    with pytest.raises(ValueError, match="different"):
        await repo.materialize("alice", parent, upstream=parent, upstream_sha=fork)
    with pytest.raises(ValueError, match="does not fork at"):
        await repo.materialize("alice", child, upstream=parent, upstream_sha=fork)

    assert git(remote, "rev-parse", child) == later_parent


async def test_accept_rejects_a_proposal_branch_that_moved_after_review(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", "moved-review-parent")
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Review plan\n")},
        process_id="moved-review-parent",
        operation_id="plan",
    )
    child_process = "moved-review-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    first_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/result.md": workspace_files.File(b"Reviewed version\n")},
        process_id=child_process,
        operation_id="reviewed",
    )
    first = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=first_head,
        process_id=child_process,
        summary="Reviewed result",
        upstream=parent,
    )
    reviewed_sha = git(remote, "rev-parse", first.proposal)
    second_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/result.md": workspace_files.File(b"Changed after review\n")},
        process_id=child_process,
        operation_id="changed",
    )
    await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=second_head,
        process_id=child_process,
        summary="Changed result",
        upstream=parent,
    )

    with pytest.raises(ValueError, match="proposal changed"):
        await repo.accept(
            "alice",
            parent,
            first.proposal,
            head=parent_head,
            proposal_sha=reviewed_sha,
            child_process_id=child_process,
            task_id="moved-review",
        )

    assert git(remote, "rev-parse", parent) == parent_head


async def test_accept_rejects_a_valid_proposal_from_the_wrong_child(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", "wrong-child-parent")
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Parent plan\n")},
        process_id="wrong-child-parent",
        operation_id="plan",
    )
    actual_process = "actual-proposal-child"
    actual_child = repo.thread_branch("alice", actual_process)
    actual_files = await repo.materialize("alice", actual_child, upstream=parent)
    actual_head = await repo.checkpoint(
        "alice",
        actual_child,
        {**actual_files, "self/actual.md": workspace_files.File(b"Actual child\n")},
        process_id=actual_process,
        operation_id="actual",
    )
    proposed = await repo.consolidate(
        "alice",
        actual_child,
        "workspace",
        head=actual_head,
        process_id=actual_process,
        summary="Actual child result",
        upstream=parent,
    )
    expected_process = "different-expected-child"
    expected_child = repo.thread_branch("alice", expected_process)
    await repo.materialize("alice", expected_child, upstream=parent)

    with pytest.raises(ValueError, match="expected child process"):
        await repo.accept(
            "alice",
            parent,
            proposed.proposal,
            head=parent_head,
            proposal_sha=git(remote, "rev-parse", proposed.proposal),
            child_process_id=expected_process,
            task_id="wrong-child",
        )

    assert git(remote, "rev-parse", parent) == parent_head


@pytest.mark.parametrize(
    ("trailer", "forged"),
    [
        ("Rotor-Process", "another-process"),
        ("Rotor-Thread", "proposal-base"),
        ("Rotor-Operation", "0" * 64),
    ],
    ids=["process", "thread", "operation"],
)
async def test_proposal_audit_rejects_forged_process_thread_and_operation(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], trailer: str, forged: str
) -> None:
    repo, remote = workspace
    branch = await propose(
        repo,
        "alice",
        {"self/audited.md": workspace_files.File(b"Audited content\n")},
        section="workspace",
        process_id="audit-binding",
        summary="Audited proposal",
    )
    proposal = await repo.inspect_proposal(branch)
    if forged == "proposal-base":
        forged = proposal.base
    message = git(remote, "show", "-s", "--format=%B", branch)
    original = next(line for line in message.splitlines() if line.startswith(f"{trailer}: "))
    forged_message = message.replace(original, f"{trailer}: {forged}")
    tree = git(remote, "show", "-s", "--format=%T", branch)
    bad = git(remote, "commit-tree", tree, "-p", proposal.base, data=forged_message.encode())
    git(remote, "update-ref", f"refs/heads/{branch}", bad)

    with pytest.raises(ValueError, match="proposal"):
        await repo.inspect_proposal(branch)


async def test_proposal_rejects_a_forged_accepted_parent_without_thread_acceptance(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    branch = await propose(
        repo,
        "alice",
        {"self/primary.md": workspace_files.File(b"Primary result\n")},
        section="workspace",
        process_id="forged-accept-primary",
        summary="Primary result",
    )
    unrelated = await propose(
        repo,
        "alice",
        {"self/unrelated.md": workspace_files.File(b"Unrelated result\n")},
        section="workspace",
        process_id="forged-accept-unrelated",
        summary="Unrelated result",
    )
    proposal = await repo.inspect_proposal(branch)
    unrelated_sha = git(remote, "rev-parse", unrelated)
    message = git(remote, "show", "-s", "--format=%B", branch)
    forged_message = f"{message}\nRotor-Accepted: {unrelated} {unrelated_sha}\n"
    tree = git(remote, "show", "-s", "--format=%T", branch)
    forged = git(
        remote,
        "commit-tree",
        tree,
        "-p",
        proposal.base,
        "-p",
        unrelated_sha,
        data=forged_message.encode(),
    )
    git(remote, "update-ref", f"refs/heads/{branch}", forged)

    with pytest.raises(ValueError, match="accepted-proposal audit"):
        await repo.inspect_proposal(branch)


async def test_accept_retry_recovers_an_uncertain_successful_push(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", "accept-replay-parent")
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        {**parent_files, "self/plan.md": workspace_files.File(b"Parent plan\n")},
        process_id="accept-replay-parent",
        operation_id="plan",
    )
    child_process = "accept-replay-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/recovered.md": workspace_files.File(b"Recovered result\n")},
        process_id=child_process,
        operation_id="result",
    )
    proposed = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_head,
        process_id=child_process,
        summary="Recover acceptance",
        upstream=parent,
    )
    proposal_sha = git(remote, "rev-parse", proposed.proposal)
    original_run = subprocess.run
    lost = False

    def lose_parent_push_response(args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        nonlocal lost
        result = original_run(args, **kwargs)
        if (
            "push" in args
            and not lost
            and any(str(arg).endswith(f":refs/heads/{parent}") for arg in args)
        ):
            lost = True
            return subprocess.CompletedProcess(args, 1, b"", b"response lost after push")
        return result

    monkeypatch.setattr(subprocess, "run", lose_parent_push_response)
    with pytest.raises(workspace_git.MainChanged, match="advanced"):
        await repo.accept(
            "alice",
            parent,
            proposed.proposal,
            head=parent_head,
            proposal_sha=proposal_sha,
            child_process_id=child_process,
            task_id="replay-task",
        )
    monkeypatch.setattr(subprocess, "run", original_run)
    revised_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/recovered.md": workspace_files.File(b"Revised after acceptance\n")},
        process_id=child_process,
        operation_id="revised-result",
    )
    revised = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=revised_head,
        process_id=child_process,
        summary="Revise accepted result",
        upstream=parent,
    )
    assert git(remote, "rev-parse", revised.proposal) != proposal_sha

    replay = await repo.accept(
        "alice",
        parent,
        proposed.proposal,
        head=parent_head,
        proposal_sha=proposal_sha,
        child_process_id=child_process,
        task_id="replay-task",
    )

    assert lost and replay.sha == git(remote, "rev-parse", parent)
    assert replay.changed == ("self/recovered.md",)
    assert replay.files["self/recovered.md"].text == "Recovered result\n"
    assert git(remote, "rev-list", "--first-parent", "--count", f"{parent_head}..{parent}") == "1"


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("self/recovered.md", b"Unreviewed replacement\n"),
        ("wiki/guide.md", b"Unreviewed wiki edit\n"),
    ],
    ids=("proposal-section", "outside-proposal-section"),
)
async def test_accept_replay_rejects_a_forged_acceptance_tree(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path], path: str, content: bytes
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", f"forged-replay-parent-{path.split('/')[0]}")
    parent_files = await repo.materialize("alice", parent)
    parent_head = await repo.checkpoint(
        "alice",
        parent,
        parent_files,
        process_id=f"forged-replay-parent-{path.split('/')[0]}",
        operation_id="parent",
    )
    child_process = f"forged-replay-child-{path.split('/')[0]}"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/recovered.md": workspace_files.File(b"Reviewed result\n")},
        process_id=child_process,
        operation_id="result",
    )
    proposed = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_head,
        process_id=child_process,
        summary="Reviewed result",
        upstream=parent,
    )
    proposal_sha = git(remote, "rev-parse", proposed.proposal)
    accepted = await repo.accept(
        "alice",
        parent,
        proposed.proposal,
        head=parent_head,
        proposal_sha=proposal_sha,
        child_process_id=child_process,
        task_id="forged-replay",
    )
    attacker_process = f"forged-replay-attacker-{path.split('/')[0]}"
    attacker = repo.thread_branch("alice", attacker_process)
    attacker_files = await repo.materialize(
        "alice", attacker, upstream=parent, upstream_sha=accepted.sha
    )
    attacker_head = await repo.checkpoint(
        "alice",
        attacker,
        {**attacker_files, path: workspace_files.File(content)},
        process_id=attacker_process,
        operation_id="tamper",
    )
    forged = git(
        remote,
        "commit-tree",
        git(remote, "show", "-s", "--format=%T", attacker_head),
        "-p",
        parent_head,
        "-p",
        proposal_sha,
        data=git(remote, "show", "-s", "--format=%B", accepted.sha).encode(),
    )
    git(remote, "update-ref", f"refs/heads/{parent}", forged)

    with pytest.raises(workspace_git.MainChanged, match="advanced"):
        await repo.accept(
            "alice",
            parent,
            proposed.proposal,
            head=parent_head,
            proposal_sha=proposal_sha,
            child_process_id=child_process,
            task_id="forged-replay",
        )


async def test_recursive_accepted_proposals_remain_ancestors_after_main_merge(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    root_process = "recursive-root"
    root = repo.thread_branch("alice", root_process)
    await repo.materialize("alice", root)
    root_head = git(remote, "rev-parse", root)
    child_process = "recursive-child"
    child = repo.thread_branch("alice", child_process)
    await repo.materialize("alice", child, upstream=root)
    child_head = git(remote, "rev-parse", child)
    grandchild_process = "recursive-grandchild"
    grandchild = repo.thread_branch("alice", grandchild_process)
    grandchild_files = await repo.materialize("alice", grandchild, upstream=child)
    grandchild_head = await repo.checkpoint(
        "alice",
        grandchild,
        {**grandchild_files, "self/nested.md": workspace_files.File(b"Nested result\n")},
        process_id=grandchild_process,
        operation_id="nested-result",
    )
    grandchild_outcome = await repo.consolidate(
        "alice",
        grandchild,
        "workspace",
        head=grandchild_head,
        process_id=grandchild_process,
        summary="Nested result",
        upstream=child,
    )
    grandchild_proposal = git(remote, "rev-parse", grandchild_outcome.proposal)
    child_accept = await repo.accept(
        "alice",
        child,
        grandchild_outcome.proposal,
        head=child_head,
        proposal_sha=grandchild_proposal,
        child_process_id=grandchild_process,
        task_id="nested-task",
    )
    child_outcome = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_accept.sha,
        process_id=child_process,
        summary="Carry nested result",
        upstream=root,
    )
    child_proposal = git(remote, "rev-parse", child_outcome.proposal)
    child_audit = await repo.inspect_proposal(child_outcome.proposal)
    assert child_audit.accepted == ((grandchild_outcome.proposal, grandchild_proposal),)
    assert git(remote, "show", "-s", "--format=%P", child_proposal).split() == [
        root_head,
        grandchild_proposal,
    ]

    root_accept = await repo.accept(
        "alice",
        root,
        child_outcome.proposal,
        head=root_head,
        proposal_sha=child_proposal,
        child_process_id=child_process,
        task_id="child-task",
    )
    root_outcome = await repo.consolidate(
        "alice",
        root,
        "workspace",
        head=root_accept.sha,
        process_id=root_process,
        summary="Publish recursive result",
    )
    root_proposal = git(remote, "rev-parse", root_outcome.proposal)
    assert (await repo.inspect_proposal(root_outcome.proposal)).accepted == (
        (child_outcome.proposal, child_proposal),
    )
    main = await workspace_review.LocalReview(repo).merge(root_outcome.proposal)

    assert git(remote, "merge-base", "--is-ancestor", root_proposal, main) == ""
    assert git(remote, "merge-base", "--is-ancestor", child_proposal, main) == ""
    assert git(remote, "merge-base", "--is-ancestor", grandchild_proposal, main) == ""
    assert git(remote, "show", "main:agents/alice/nested.md") == "Nested result"


async def test_delete_proposal_keeps_a_branch_accepted_by_its_parent_upstream(
    workspace: tuple[workspace_repo.WorkspaceRepo, pathlib.Path],
) -> None:
    repo, remote = workspace
    parent = repo.thread_branch("alice", "delete-parent")
    await repo.materialize("alice", parent)
    parent_head = git(remote, "rev-parse", parent)
    child_process = "delete-child"
    child = repo.thread_branch("alice", child_process)
    child_files = await repo.materialize("alice", child, upstream=parent)
    child_head = await repo.checkpoint(
        "alice",
        child,
        {**child_files, "self/keep-child.md": workspace_files.File(b"Keep accepted proposal\n")},
        process_id=child_process,
        operation_id="keep",
    )
    proposed = await repo.consolidate(
        "alice",
        child,
        "workspace",
        head=child_head,
        process_id=child_process,
        summary="Keep child proposal",
        upstream=parent,
    )
    proposal_sha = git(remote, "rev-parse", proposed.proposal)
    await repo.accept(
        "alice",
        parent,
        proposed.proposal,
        head=parent_head,
        proposal_sha=proposal_sha,
        child_process_id=child_process,
        task_id="keep-task",
    )

    assert not await repo.delete_proposal(proposed.proposal, upstream=parent)
    assert git(remote, "rev-parse", proposed.proposal) == proposal_sha


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:acme/workspace.git",
        "https://token@github.com/acme/workspace.git",
        "https://github.com.evil.test/acme/workspace",
        "https://github.com/acme/workspace?token=secret",
        "ext::sh -c false",
        "file://host/tmp/workspace.git",
        "https://github.com:443/acme/workspace",
    ],
)
def test_remote_transport_and_embedded_credentials_are_rejected(remote: str) -> None:
    with pytest.raises(ValueError):
        workspace_repo.WorkspaceRepo(remote)
