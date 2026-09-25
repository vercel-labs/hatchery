"""Read-only, bounded views of committed repository trees and individual file diffs."""

import asyncio
import dataclasses
import re
import typing

from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git
from hatchery.workspace import repo as workspace_repo

PREVIEW_BYTES = 256 * 1024


def validate_branch(branch: str) -> None:
    if branch != "main" and not re.fullmatch(
        r"(?:threads/[a-z0-9_-]+|consolidations/[a-z0-9_-]+/(?:workspace|wiki))/[0-9a-f]{64}",
        branch,
    ):
        raise ValueError("not a workspace branch")


class RepositoryBrowser:
    def __init__(self, repo: workspace_repo.WorkspaceRepo):
        self.repo = repo

    async def inspect(
        self,
        branch: str = "main",
        *,
        base: str | None = None,
        path: str | None = None,
        revision: str | None = None,
    ) -> dict[str, typing.Any]:
        validate_branch(branch)
        for sha in (base, revision):
            if sha is not None and not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("revision must be a Git commit SHA")
        if path is not None:
            workspace_files.validate_repo_path(path)
        token = await self.repo.credential()

        def work() -> dict[str, typing.Any]:
            with workspace_git.Git(self.repo.remote, token) as git:
                main = git.fetch("main")
                assert main is not None
                head = main if branch == "main" else git.fetch(branch, optional=True)
                # A queued thread may not have created its branch yet.
                if head is None:
                    raise FileNotFoundError("Thread has no checkpoint yet")
                if revision is not None:
                    if not git.is_ancestor(revision, head):
                        raise workspace_git.GitError(
                            "Selected revision is no longer on this branch; refresh"
                        )
                    head = revision
                proposal = None
                if branch.startswith("consolidations/"):
                    proposal = self.repo._proposal(git, branch, head)
                    baseline = proposal.base
                elif branch == "main":
                    baseline = head
                else:
                    baseline = base or git.merge_base(main, head)
                    if not git.is_ancestor(baseline, head):
                        raise ValueError("thread baseline is not an ancestor")
                before, after = git.entries(baseline), git.entries(head)
                if branch.startswith("threads/") and base is None:
                    owner = branch.split("/", 2)[1]
                    prefix = f"agents/{owner}/"
                    # Thread commits contain only their agent's directory and shared wiki.
                    # Fill other agents from current main so a latest comparison does not
                    # misreport another agent's newer files as deletions.
                    after = {
                        path: entry
                        for path, entry in after.items()
                        if not path.startswith("agents/") or path.startswith(prefix)
                    }
                    after.update(
                        {
                            path: entry
                            for path, entry in git.entries(main).items()
                            if path.startswith("agents/") and not path.startswith(prefix)
                        }
                    )
                if max(len(before), len(after)) > workspace_files.MAX_FILES:
                    raise ValueError("repository file count exceeds preview limit")
                paths = sorted(before.keys() | after.keys())
                changed = [p for p in paths if before.get(p) != after.get(p)]
                merged = bool(proposal) and git.is_ancestor(head, main)
                common: dict[str, typing.Any] = {
                    "branch": branch,
                    "sha": head,
                    "base_sha": baseline,
                    "main_sha": main,
                    "merged": merged,
                }
                if path is not None:
                    if path not in before and path not in after:
                        raise FileNotFoundError("File is not present in this revision or its base")

                    def preview(entries: dict[str, typing.Any]) -> dict[str, typing.Any] | None:
                        entry = entries.get(path)
                        if entry is None:
                            return None
                        value = dataclasses.asdict(entry)
                        if entry.mode not in ("100644", "100755"):
                            return {
                                **value,
                                "text": None,
                                "notice": "Special file; preview unavailable",
                            }
                        if entry.size > PREVIEW_BYTES:
                            return {
                                **value,
                                "text": None,
                                "notice": "File exceeds 256 KiB preview limit",
                            }
                        content = git.blob(entry.oid)
                        try:
                            text = content.decode("utf-8")
                            if "\0" in text:
                                raise UnicodeError()
                        except UnicodeError:
                            return {**value, "text": None, "notice": "Binary file"}
                        return {**value, "text": text, "notice": None}

                    old, new = preview(before), preview(after)
                    diff = None
                    truncated = False
                    if all(v is None or v["text"] is not None for v in (old, new)):
                        diff = git.diff(path, before.get(path), after.get(path)).decode(
                            "utf-8", "replace"
                        )
                        truncated = len(diff) > PREVIEW_BYTES
                        diff = diff[:PREVIEW_BYTES]
                    return {
                        **common,
                        "path": path,
                        "before": old,
                        "after": new,
                        "diff": diff,
                        "diff_truncated": truncated,
                    }
                return {
                    **common,
                    "files": [
                        {"path": p, **dataclasses.asdict(e)} for p, e in sorted(after.items())
                    ],
                    "changes": [
                        {
                            "path": p,
                            "status": "added"
                            if p not in before
                            else "deleted"
                            if p not in after
                            else "modified",
                            "before_mode": before[p].mode if p in before else None,
                            "after_mode": after[p].mode if p in after else None,
                        }
                        for p in changed
                    ],
                    "summary": proposal.summary if proposal else "",
                }

        return await asyncio.to_thread(work)
