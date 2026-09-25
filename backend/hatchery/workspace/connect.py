"""Storage-repo GitHub credentials from Vercel Connect, with static-token fallback.

These are the storage repository's credentials (the Connect GitHub App identity).
Coding checkouts keep Hatchery's own GitHub connection behavior.
"""

import collections.abc
import dataclasses
import os

from vercel import connect as vercel_connect

from hatchery.workspace import git as workspace_git
from hatchery.workspace import repo as workspace_repo


@dataclasses.dataclass(frozen=True)
class ConnectGitHubToken:
    """Mint short-lived GitHub App tokens scoped to exact repositories of one installation."""

    connector: str
    installation_id: str | None = None

    def __post_init__(self) -> None:
        workspace_git.validate_text(self.connector, "GitHub connector")
        if self.installation_id is not None:
            workspace_git.validate_text(self.installation_id, "GitHub installation ID")

    async def resolve(self, org: str, repositories: collections.abc.Sequence[str]) -> str:
        """Resolve a token for the named repositories of one GitHub account."""
        names = tuple(sorted(set(repositories)))
        if not names:
            raise ValueError("at least one GitHub repository is required")
        workspace_git.validate_text(org, "GitHub organization", maximum=100)
        for repository in names:
            workspace_git.validate_text(repository, "GitHub repository", maximum=100)
        return await vercel_connect.get_token(
            self.connector,
            subject=vercel_connect.ConnectAppTokenSubject(),
            installation_id=self.installation_id,
            authorization_details=[
                vercel_connect.ConnectGitHubAppInstallationAuthorizationDetail(
                    org=org,
                    permissions=("contents:write", "pull_requests:write"),
                    repositories=names,
                )
            ],
        )

    def for_remote(self, remote: str) -> workspace_repo.TokenProvider:
        """A credential for one github.com remote, resolved immediately before each use."""
        kind, repository = workspace_git.parse_remote(remote)
        if kind != "github":
            raise ValueError("Vercel Connect GitHub credentials require a github.com remote")
        org, name = repository.split("/", 1)

        async def credential() -> str:
            return await self.resolve(org, (name,))

        return credential


def storage_remote() -> str:
    """The storage repository remote from `HATCHERY_STORAGE_REPO`.

    `org/repo` means `https://github.com/org/repo.git`; a full remote URL (such as a
    local `file://` bare repository) is used as is.
    """
    value = os.environ["HATCHERY_STORAGE_REPO"]
    remote = value if "://" in value else f"https://github.com/{value}.git"
    workspace_git.parse_remote(remote)
    return remote


def github_credentials(remote: str) -> str | workspace_repo.TokenProvider | None:
    """Prefer Vercel Connect when configured; otherwise use `GITHUB_TOKEN`."""
    kind, _ = workspace_git.parse_remote(remote)
    if kind != "github":
        return None
    connector = os.getenv("GITHUB_CONNECTOR")
    if connector:
        token = ConnectGitHubToken(connector, os.getenv("HATCHERY_GITHUB_INSTALLATION_ID"))
        return token.for_remote(remote)
    return os.getenv("GITHUB_TOKEN")
