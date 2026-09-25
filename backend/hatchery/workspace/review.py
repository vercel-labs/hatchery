"""How consolidation proposals get reviewed and merged into main.

`LocalReview` merges through the worker's own Git; `GitHubReview` opens pull
requests. Credentials never leave the worker.
"""

import asyncio
import collections.abc
import dataclasses
import typing
import urllib.parse

import httpx

from hatchery.workspace import git as workspace_git
from hatchery.workspace import repo as workspace_repo


@dataclasses.dataclass(frozen=True)
class ReviewResult:
    url: str
    merged: bool
    branch: str


class Review(typing.Protocol):
    async def submit(
        self, *, branch: str, summary: str, owner: str, section: str, policy: str
    ) -> ReviewResult: ...

    async def withdraw(self, branch: str) -> bool:
        """Close and remove an unmerged proposal whose contribution has been withdrawn."""
        ...


type Requester = collections.abc.Callable[..., collections.abc.Awaitable[httpx.Response]]


class ReviewRequired(workspace_git.GitError):
    """The reviewer refused an automatic merge, for example because of branch protection."""


def _policy(policy: str) -> None:
    if policy not in ("auto", "review"):
        raise ValueError("merge policy must be auto or review")


class LocalReview:
    def __init__(self, repo: workspace_repo.WorkspaceRepo):
        if workspace_git.parse_remote(repo.remote)[0] != "file":
            raise ValueError("LocalReview requires a file:// workspace remote")
        self.repo = repo

    async def submit(
        self, *, branch: str, summary: str, owner: str, section: str, policy: str
    ) -> ReviewResult:
        _policy(policy)
        proposal = await self.repo.inspect_proposal(branch)
        if (owner, section) != (proposal.owner, proposal.section):
            raise ValueError("proposal owner or section does not match the branch")
        # The committed summary is the audit authority, not a potentially changed retry input.
        if policy == "auto":
            await self.merge(branch)
        return ReviewResult(
            f"{self.repo.remote}#refs/heads/{urllib.parse.quote(branch, safe='/')}",
            policy == "auto",
            branch,
        )

    async def merge(self, branch: str, *, expected_sha: str | None = None) -> str:
        """Explicit operator approval; retain the branch and create an auditable merge commit."""
        return await self.repo.merge(branch, expected_sha=expected_sha)

    async def withdraw(self, branch: str) -> bool:
        return await self.repo.delete_proposal(branch)


class GitHubReview:
    def __init__(
        self, repo: workspace_repo.WorkspaceRepo, *, client: httpx.AsyncClient | None = None
    ):
        kind, repository = workspace_git.parse_remote(repo.remote)
        if kind != "github":
            raise ValueError("GitHubReview requires an HTTPS github.com workspace remote")
        self.repo = repo
        self.repository = repository
        self._client = client

    async def _session(self) -> tuple[httpx.AsyncClient, dict[str, str], Requester]:
        if not self.repo.has_credentials:
            raise ValueError("GitHub proposal operations require a worker token")
        token = await self.repo.credential()
        if token is None:
            raise ValueError("GitHub proposal operations require a worker token")
        client = self._client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async def request(method: str, url: str, **kwargs: typing.Any) -> httpx.Response:
            for attempt in range(workspace_git.MAX_ATTEMPTS):
                try:
                    response = await client.request(
                        method, url, headers=headers, follow_redirects=False, timeout=30, **kwargs
                    )
                except httpx.TransportError:
                    if attempt == workspace_git.MAX_ATTEMPTS - 1:
                        raise workspace_git.GitError(
                            "GitHub request failed after bounded retries"
                        ) from None
                else:
                    if response.status_code != 429 and response.status_code < 500:
                        return response
                    if attempt == workspace_git.MAX_ATTEMPTS - 1:
                        raise workspace_git.GitError(
                            f"GitHub unavailable (HTTP {response.status_code})"
                        )
                await asyncio.sleep(0.05 * (2**attempt))
            raise AssertionError("unreachable")

        return client, headers, request

    async def _open_pulls(self, request: Requester, branch: str) -> list[dict[str, typing.Any]]:
        account = self.repository.split("/", 1)[0]
        found = await request(
            "GET",
            f"https://api.github.com/repos/{self.repository}/pulls",
            params={
                "head": f"{account}:{branch}",
                "base": "main",
                "state": "open",
                "per_page": 100,
            },
        )
        if found.status_code != 200:
            raise workspace_git.GitError(f"GitHub PR lookup failed (HTTP {found.status_code})")
        return [
            item
            for item in found.json()
            if item.get("head", {}).get("ref") == branch
            and item.get("head", {}).get("repo", {}).get("full_name", "").lower()
            == self.repository.lower()
            and item.get("base", {}).get("ref") == "main"
        ]

    async def withdraw(self, branch: str) -> bool:
        """Drop the branch of an unmerged proposal, then close its pull request."""
        client, _, request = await self._session()
        endpoint = f"https://api.github.com/repos/{self.repository}/pulls"
        try:
            pulls = await self._open_pulls(request, branch)
            if not await self.repo.delete_proposal(branch):
                return False
            for pr in pulls:
                closed = await request(
                    "PATCH", f"{endpoint}/{int(pr['number'])}", json={"state": "closed"}
                )
                if closed.status_code != 200:
                    raise workspace_git.GitError(
                        f"GitHub PR close failed (HTTP {closed.status_code})"
                    )
            return True
        finally:
            if self._client is None:
                await client.aclose()

    async def submit(
        self, *, branch: str, summary: str, owner: str, section: str, policy: str
    ) -> ReviewResult:
        _policy(policy)
        proposal = await self.repo.inspect_proposal(branch)
        if (owner, section) != (proposal.owner, proposal.section):
            raise ValueError("proposal owner or section does not match the branch")
        client, headers, request = await self._session()
        endpoint = f"https://api.github.com/repos/{self.repository}/pulls"

        try:
            pr: dict[str, typing.Any] | None = None
            # Never blindly retry POST: its outcome may be unknown. Look up the stable head first.
            for attempt in range(workspace_git.MAX_ATTEMPTS):
                candidates = await self._open_pulls(request, branch)
                if len(candidates) > 1:
                    raise workspace_git.GitError(
                        "multiple PRs exist for this operation; operator review required"
                    )
                if candidates:
                    detail = await request("GET", f"{endpoint}/{int(candidates[0]['number'])}")
                    if detail.status_code != 200:
                        raise workspace_git.GitError(
                            f"GitHub PR read failed (HTTP {detail.status_code})"
                        )
                    pr = detail.json()
                    break
                try:
                    created = await client.post(
                        endpoint,
                        headers=headers,
                        follow_redirects=False,
                        timeout=30,
                        json={
                            "title": proposal.summary,
                            "head": branch,
                            "base": "main",
                            "body": (
                                f"Hatchery {section} consolidation for `{owner}`.\n\n"
                                f"Operation branch: `{branch}`\nBase: `{proposal.base}`"
                            ),
                        },
                    )
                except httpx.TransportError:
                    pass
                else:
                    if created.status_code == 201:
                        pr = created.json()
                        break
                    if created.status_code not in (409, 422, 429) and created.status_code < 500:
                        raise workspace_git.GitError(
                            f"GitHub PR creation failed (HTTP {created.status_code})"
                        )
                await asyncio.sleep(0.05 * (2**attempt))
            if pr is None:
                raise workspace_git.GitError(
                    "GitHub proposal creation could not be confirmed after bounded retries"
                )
            number = int(pr["number"])
            url = f"https://github.com/{self.repository}/pull/{number}"
            if (
                pr.get("base", {}).get("ref") != "main"
                or pr.get("head", {}).get("ref") != branch
                or pr.get("head", {}).get("repo", {}).get("full_name", "").lower()
                != self.repository.lower()
                or pr.get("head", {}).get("sha") != proposal.sha
            ):
                raise ReviewRequired(
                    f"proposal target or head changed; refusing unverified merge: {url}"
                )
            if pr.get("merged") or pr.get("merged_at"):
                return ReviewResult(url, True, branch)
            if pr.get("state") != "open":
                raise ReviewRequired(f"proposal was closed without merging: {url}")
            if policy == "review":
                return ReviewResult(url, False, branch)
            response = await request(
                "PUT",
                f"{endpoint}/{number}/merge",
                json={"sha": proposal.sha, "merge_method": "merge"},
            )
            if response.status_code == 200 and response.json().get("merged") is True:
                return ReviewResult(url, True, branch)
            # A previous PUT may have merged before its response was lost.
            detail = await request("GET", f"{endpoint}/{number}")
            if detail.status_code == 200 and detail.json().get("merged") is True:
                return ReviewResult(url, True, branch)
            raise ReviewRequired(
                f"GitHub did not merge the proposal (HTTP {response.status_code}): {url}"
            )
        finally:
            if self._client is None:
                await client.aclose()
