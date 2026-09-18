"""User-owned GitHub and Slack connections."""

import datetime
import logging
import os

import fastapi
import httpx
from vercel import connect

import auth
from store import auth as auth_store

GITHUB_API = "https://api.github.com"
SLACK_API = "https://slack.com/api"

log = logging.getLogger("connections")


class ConnectionRequired(RuntimeError):
    """A connected provider grant is missing or expired."""


_CONNECT_ERRORS = (
    connect.UserAuthorizationRequiredError,
    connect.NoValidTokenError,
    connect.ConnectorInstallationRequiredError,
)


def _github_connector() -> str:
    return os.environ["GITHUB_CONNECTOR"]


def _github_subject(user: dict) -> connect.ConnectUserTokenSubject:
    return connect.ConnectUserTokenSubject(id=user["id"])


def github_connection(user: dict) -> dict | None:
    saved = user.get("github")
    return saved if isinstance(saved, dict) else None


async def begin_github(request: fastapi.Request, user: dict) -> fastapi.responses.RedirectResponse:
    try:
        authorization = await connect.start_authorization(
            _github_connector(),
            subject=_github_subject(user),
            return_url=f"{auth.request_origin(request)}/api/connections/github/return",
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("GitHub authorization is required") from error
    return fastapi.responses.RedirectResponse(authorization.url)


async def finish_github(user: dict) -> fastapi.responses.RedirectResponse:
    try:
        token = await connect.get_token_response(
            _github_connector(), subject=_github_subject(user)
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("GitHub authorization was not completed") from error
    async with httpx.AsyncClient(
        base_url=GITHUB_API,
        timeout=30,
        headers={
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {token.token}",
            "x-github-api-version": "2022-11-28",
        },
    ) as http:
        response = await http.get("/user")
    if response.status_code >= 300:
        raise fastapi.HTTPException(502, "GitHub identity lookup failed")
    profile = response.json()
    connection = {
        "id": str(profile["id"]),
        "login": profile["login"],
        "name": profile.get("name"),
        "avatar_url": profile.get("avatar_url"),
        "installation_id": token.installation_id,
        "connected_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    try:
        await auth_store.save_github_connection(user["id"], connection)
    except auth_store.GitHubIdentityConflict as error:
        raise fastapi.HTTPException(409, "GitHub account is already connected") from error
    return fastapi.responses.RedirectResponse("/")


async def disconnect_github(user: dict) -> None:
    connection = github_connection(user) or {}
    try:
        await connect.revoke_token(
            _github_connector(),
            subject=_github_subject(user),
            installation_id=connection.get("installation_id"),
        )
    except _CONNECT_ERRORS:
        pass
    except Exception:
        log.exception("GitHub token revocation failed", extra={"user_id": user["id"]})
    await auth_store.delete_github_connection(user["id"])


def _slack_connector() -> str:
    return os.environ["SLACK_CONNECTOR"]


def slack_connection(user: dict) -> dict | None:
    saved = user.get("slack")
    return saved if isinstance(saved, dict) else None


async def begin_slack(request: fastapi.Request, user: dict) -> fastapi.responses.RedirectResponse:
    try:
        authorization = await connect.start_authorization(
            _slack_connector(),
            subject=_github_subject(user),
            return_url=f"{auth.request_origin(request)}/api/connections/slack/return",
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("Slack authorization is required") from error
    return fastapi.responses.RedirectResponse(authorization.url)


async def finish_slack(user: dict) -> fastapi.responses.RedirectResponse:
    try:
        token = await connect.get_token_response(
            _slack_connector(), subject=_github_subject(user)
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("Slack authorization was not completed") from error
    profile = token.metadata or {}
    team_id = token.tenant_id or profile.get("team_id")
    slack_user_id = token.external_subject or profile.get("user_id")
    if not team_id or not slack_user_id:
        async with httpx.AsyncClient(base_url=SLACK_API, timeout=30) as http:
            response = await http.post(
                "/auth.test", headers={"authorization": f"Bearer {token.token}"}
            )
        profile = response.json()
        if response.status_code >= 300 or not profile.get("ok"):
            log.error(
                "Slack identity lookup failed",
                extra={
                    "user_id": user["id"],
                    "slack_error": profile.get("error"),
                    "token_id": token.token_id,
                },
            )
            raise fastapi.HTTPException(502, "Slack identity lookup failed")
        team_id = profile.get("team_id")
        slack_user_id = profile.get("user_id")
    connection = {
        "team_id": str(team_id),
        "team": profile.get("team") or profile.get("team_name"),
        "user_id": str(slack_user_id),
        "user": profile.get("user") or profile.get("user_name") or token.name,
        "connected_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    try:
        await auth_store.save_slack_connection(user["id"], connection)
    except auth_store.SlackIdentityConflict as error:
        raise fastapi.HTTPException(409, "Slack account is already connected") from error
    return fastapi.responses.RedirectResponse("/")


async def slack_token(user_id: str) -> str:
    try:
        return await connect.get_token(
            _slack_connector(), subject=connect.ConnectUserTokenSubject(id=user_id)
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("connect Slack before using Slack") from error


async def disconnect_slack(user: dict) -> None:
    try:
        await connect.revoke_token(_slack_connector(), subject=_github_subject(user))
    except _CONNECT_ERRORS:
        pass
    except Exception:
        log.exception("Slack token revocation failed", extra={"user_id": user["id"]})
    await auth_store.delete_slack_connection(user["id"])


async def github_identity(user_id: str) -> dict | None:
    user = await auth_store.get_user(user_id)
    return github_connection(user or {})


async def github_token(user_id: str, installation_id: str | None = None) -> str:
    if installation_id is None:
        connection = await github_identity(user_id) or {}
        installation_id = connection.get("installation_id")
    try:
        return await connect.get_token(
            _github_connector(),
            subject=connect.ConnectUserTokenSubject(id=user_id),
            installation_id=installation_id,
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("connect GitHub before accessing repositories") from error


async def github_repositories(user_id: str) -> list[dict]:
    """List repositories available to the user's current connector installation."""
    connection = await github_identity(user_id)
    if connection is None:
        raise ConnectionRequired("connect GitHub before choosing a memory repository")
    installation_id = connection.get("installation_id")
    if not installation_id:
        raise ConnectionRequired("install the Hatchery connector before choosing a repository")
    try:
        token = await connect.get_token(
            _github_connector(),
            subject=connect.ConnectAppTokenSubject(),
            installation_id=installation_id,
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("install the Hatchery connector before choosing a repository") from error
    headers = {
        "accept": "application/vnd.github+json",
        "authorization": f"Bearer {token}",
        "x-github-api-version": "2022-11-28",
    }
    found: list[dict] = []
    page = 1
    async with httpx.AsyncClient(
        base_url=GITHUB_API, timeout=30, headers=headers, follow_redirects=True
    ) as http:
        while True:
            response = await http.get(
                "/installation/repositories", params={"per_page": 100, "page": page}
            )
            if response.status_code in {401, 403}:
                raise ConnectionRequired("GitHub connector installation is required")
            if response.status_code >= 300:
                raise RuntimeError("GitHub repository lookup failed")
            batch = response.json().get("repositories", [])
            for repository in batch:
                full_name = repository.get("full_name")
                if not isinstance(full_name, str) or "/" not in full_name:
                    continue
                found.append(
                    {
                        "full_name": full_name,
                        "installation_id": installation_id,
                        "private": repository.get("private") is True,
                    }
                )
            if len(batch) < 100:
                break
            page += 1
    return sorted(found, key=lambda repository: repository["full_name"].lower())


async def github_app_token(repo: str, installation_id: str | None = None) -> str:
    """Mint an app token scoped only to Hatchery's private agent repository."""
    owner, separator, name = repo.removesuffix(".git").partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("agent repository must use owner/repo form")
    try:
        return await connect.get_token(
            _github_connector(),
            subject=connect.ConnectAppTokenSubject(),
            installation_id=installation_id
            or os.environ.get("HATCHERY_AGENTS_REPOSITORY_INSTALLATION_ID"),
            authorization_details=[
                connect.ConnectGitHubAppInstallationAuthorizationDetail(
                    org=owner,
                    permissions=("contents:write",),
                    repositories=(name,),
                )
            ],
        )
    except _CONNECT_ERRORS as error:
        raise ConnectionRequired("Hatchery cannot access the agent repository") from error


async def github_repo_warning(user_id: str, repo: str) -> str | None:
    """Return why the connected GitHub app cannot make a PR to one repository."""
    connection = await github_identity(user_id)
    if connection is None:
        return f"Connect GitHub to let Hatchery make pull requests to {repo}."
    token = await github_token(user_id, connection.get("installation_id"))
    headers = {
        "accept": "application/vnd.github+json",
        "authorization": f"Bearer {token}",
        "x-github-api-version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(
            base_url=GITHUB_API, timeout=30, headers=headers, follow_redirects=True
        ) as http:
            repository = await http.get(f"/repos/{repo}")
            if repository.status_code >= 300:
                return f"Could not verify the Hatchery GitHub app for {repo}."
            canonical = str(repository.json()["full_name"])
            owner = canonical.split("/", 1)[0]
            installations = await http.get("/user/installations", params={"per_page": 100})
            if installations.status_code >= 300:
                return f"Could not verify the Hatchery GitHub app for {canonical}."
            installation = next(
                (
                    item
                    for item in installations.json().get("installations", [])
                    if str((item.get("account") or {}).get("login", "")).lower()
                    == owner.lower()
                ),
                None,
            )
            if installation is None:
                return f"Install the Hatchery GitHub app on {owner} to make pull requests to {canonical}."
            permissions = installation.get("permissions") or {}
            if permissions.get("contents") != "write" or permissions.get("pull_requests") != "write":
                return f"Give the Hatchery GitHub app contents and pull requests write access for {canonical}."
            repositories = await http.get(
                f"/user/installations/{installation['id']}/repositories",
                params={"per_page": 100},
            )
            if repositories.status_code >= 300:
                return f"Could not verify the Hatchery GitHub app for {canonical}."
            accessible = {
                str(item.get("full_name", "")).lower()
                for item in repositories.json().get("repositories", [])
            }
            if canonical.lower() not in accessible:
                return f"Give the Hatchery GitHub app access to {canonical}."
    except httpx.HTTPError:
        return f"Could not verify the Hatchery GitHub app for {repo}."
    return None
