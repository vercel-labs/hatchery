"""Git views distinguish raw thread work from curated and merged state."""

import pathlib

import pytest

from hatchery.workspace import browser as workspace_browser
from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git
from hatchery.workspace import local as workspace_local
from hatchery.workspace import repo as workspace_repo


async def propose(
    repo: workspace_repo.WorkspaceRepo,
    owner: str,
    changes: dict[str, workspace_files.File],
    *,
    section: str,
    process_id: str,
) -> str:
    """Publish a section delta the way a thread does: checkpoint, then consolidate."""
    branch = repo.thread_branch(owner, process_id)
    files = {**await repo.materialize(owner, branch), **changes}
    head = await repo.checkpoint(
        owner, branch, files, process_id=process_id, operation_id="checkpoint"
    )
    outcome = await repo.consolidate(
        owner, branch, section, head=head, process_id=process_id, summary="Proposal"
    )
    assert outcome.proposal, outcome
    return outcome.proposal


async def test_inspection_keeps_thread_and_proposal_diffs_after_main_has_merged(
    tmp_path: pathlib.Path,
) -> None:
    repo = workspace_repo.WorkspaceRepo(await workspace_local.initialize_local(tmp_path))
    await repo.join(
        "mira",
        {
            "AGENTS.md": workspace_files.File(b"Mira\n"),
            "old.md": workspace_files.File(b"Discard me\n"),
        },
    )
    branch = repo.thread_branch("mira", "research")
    files = await repo.materialize("mira", branch)
    base, _ = await repo.read_main("mira")
    del files["self/old.md"]
    files["self/note.md"] = workspace_files.File(b"Raw note\n")
    files["wiki/guide.md"] = workspace_files.File(b"Raw wiki\n")
    await repo.checkpoint("mira", branch, files, process_id="research", operation_id="checkpoint")
    browser = workspace_browser.RepositoryBrowser(repo)
    main = await browser.inspect()
    assert "agents/mira/note.md" not in [f["path"] for f in main["files"]]
    view = await browser.inspect(branch, base=base)
    assert [(c["path"], c["status"]) for c in view["changes"]] == [
        ("agents/mira/note.md", "added"),
        ("agents/mira/old.md", "deleted"),
        ("wiki/guide.md", "added"),
    ]
    raw = await browser.inspect(branch, base=base, path="agents/mira/note.md")
    assert raw["after"]["text"] == "Raw note\n"
    deleted = await browser.inspect(branch, base=base, path="agents/mira/old.md")
    assert deleted["after"] is None and "-Discard me" in deleted["diff"]
    files["wiki/guide.md"] = workspace_files.File(b"Curated shared guide\n")
    head = await repo.checkpoint(
        "mira", branch, files, process_id="research", operation_id="curate"
    )
    proposal = (
        await repo.consolidate(
            "mira", branch, "wiki", head=head, process_id="research", summary="Retain guide"
        )
    ).proposal
    pending = await browser.inspect(proposal)
    assert not pending["merged"]
    assert [c["path"] for c in pending["changes"]] == ["wiki/guide.md"]
    await repo.merge(proposal)
    merged = await browser.inspect(proposal)
    assert merged["merged"] and merged["changes"] == pending["changes"]
    assert (await browser.inspect(path="wiki/guide.md"))["after"][
        "text"
    ] == "Curated shared guide\n"
    assert [
        (c["path"], c["status"]) for c in (await browser.inspect(branch, base=base))["changes"]
    ] == [
        ("agents/mira/note.md", "added"),
        ("agents/mira/old.md", "deleted"),
        ("wiki/guide.md", "added"),
    ]


async def test_latest_thread_diff_uses_the_newest_main_merged_into_the_thread(
    tmp_path: pathlib.Path,
) -> None:
    repo = workspace_repo.WorkspaceRepo(await workspace_local.initialize_local(tmp_path))
    await repo.join("mira", {"AGENTS.md": workspace_files.File(b"Mira\n")})
    branch = repo.thread_branch("mira", "latest")
    files = await repo.materialize("mira", branch)
    base, _ = await repo.read_main("mira")
    files["self/note.md"] = workspace_files.File(b"Thread note\n")
    head = await repo.checkpoint(
        "mira", branch, files, process_id="latest", operation_id="checkpoint"
    )
    await repo.join("charlie", {"AGENTS.md": workspace_files.File(b"Charlie\n")})
    shared = await propose(
        repo,
        "charlie",
        {"wiki/shared.md": workspace_files.File(b"Shared update\n")},
        section="wiki",
        process_id="shared",
    )
    await repo.merge(shared)
    refreshed = await repo.refresh("mira", branch, head=head, base=base, process_id="latest")

    browser = workspace_browser.RepositoryBrowser(repo)
    full = await browser.inspect(branch, base=base)
    latest = await browser.inspect(branch)

    assert full["base_sha"] == base
    assert {change["path"] for change in full["changes"]} == {
        "wiki/shared.md",
        "agents/mira/note.md",
    }
    assert latest["base_sha"] == refreshed.main
    assert [change["path"] for change in latest["changes"]] == ["agents/mira/note.md"]


async def test_file_preview_is_pinned_to_the_selected_revision_and_does_not_render_binary(
    tmp_path: pathlib.Path,
) -> None:
    repo = workspace_repo.WorkspaceRepo(await workspace_local.initialize_local(tmp_path))
    await repo.join(
        "mira",
        {
            "AGENTS.md": workspace_files.File(b"First\n"),
            "data.bin": workspace_files.File(b"\0\x80binary"),
        },
    )
    browser = workspace_browser.RepositoryBrowser(repo)
    original = await browser.inspect()
    binary = await browser.inspect(path="agents/mira/data.bin")
    assert binary["after"]["text"] is None and binary["after"]["notice"] == "Binary file"
    proposal = await propose(
        repo,
        "mira",
        {"self/AGENTS.md": workspace_files.File(b"Second\n")},
        section="workspace",
        process_id="edit",
    )
    await repo.merge(proposal)
    selected = await browser.inspect(path="agents/mira/AGENTS.md", revision=original["sha"])
    assert selected["after"]["text"] == "First\n"
    assert (await browser.inspect(path="agents/mira/AGENTS.md"))["after"]["text"] == "Second\n"
    with pytest.raises(ValueError, match="unsafe workspace path"):
        await browser.inspect(path="../.env.local")
    with pytest.raises(ValueError, match="not a workspace branch"):
        await browser.inspect("--upload-pack=bad")


async def test_approval_rejects_a_different_head_than_the_operator_reviewed(
    tmp_path: pathlib.Path,
) -> None:
    repo = workspace_repo.WorkspaceRepo(await workspace_local.initialize_local(tmp_path))
    await repo.join("mira", {"AGENTS.md": workspace_files.File(b"Mira\n")})
    base, _ = await repo.read_main("mira")
    branch = await propose(
        repo,
        "mira",
        {"wiki/review.md": workspace_files.File(b"Review this\n")},
        section="wiki",
        process_id="review",
    )
    with pytest.raises(workspace_git.MainChanged, match="Proposal changed"):
        await repo.merge(branch, expected_sha=base)
    assert (await repo.read_main("mira"))[0] == base
    proposal = await repo.inspect_proposal(branch)
    await repo.merge(branch, expected_sha=proposal.sha)
    assert (await workspace_browser.RepositoryBrowser(repo).inspect(branch))["merged"] is True
