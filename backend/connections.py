"""User-owned GitHub and Vercel credentials."""

import datetime
import logging
import os

import fastapi
import httpx
from cryptography import fernet
from vercel import connect

import auth
from store import auth as auth_store

GITHUB_API = "https://api.github.com"
SLACK_API = "https://slack.com/api"
VERCEL_API = "https://api.vercel.com"
VERCEL_SECRET = "vercel_cli"

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
    await auth_store.save_github_connection(user["id"], connection)
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


def _secret_box() -> fernet.Fernet:
    value = os.environ.get("HATCHERY_CREDENTIAL_KEY", "")
    if not value:
        raise RuntimeError("HATCHERY_CREDENTIAL_KEY is required to store credentials")
    try:
        return fernet.Fernet(value.encode())
    except (ValueError, TypeError) as error:
        raise RuntimeError("HATCHERY_CREDENTIAL_KEY must be a Fernet key") from error


async def vercel_cli_connection(user_id: str) -> dict | None:
    user = await auth_store.get_user(user_id)
    saved = (user or {}).get(VERCEL_SECRET)
    return saved if isinstance(saved, dict) else None


async def vercel_cli_token(user_id: str) -> str | None:
    encrypted = await auth_store.get_secret(user_id, VERCEL_SECRET)
    if encrypted is None:
        return None
    try:
        return _secret_box().decrypt(encrypted.encode()).decode()
    except fernet.InvalidToken as error:
        raise RuntimeError("stored Vercel CLI credential cannot be decrypted") from error


async def connect_vercel_cli(user_id: str, token: str) -> dict:
    token = token.strip()
    if not token:
        raise ValueError("token is required")
    headers = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=VERCEL_API, headers=headers, timeout=30) as http:
        response = await http.get("/v2/user")
    if response.status_code in (401, 403):
        raise ValueError("Vercel rejected this token")
    response.raise_for_status()
    profile = response.json().get("user") or response.json()
    connection = {
        "user_id": str(profile["id"]),
        "username": profile.get("username"),
        "email": profile.get("email"),
        "connected_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    encrypted = _secret_box().encrypt(token.encode()).decode()
    await auth_store.save_secret(user_id, VERCEL_SECRET, encrypted)
    await auth_store.save_connection(user_id, VERCEL_SECRET, connection)
    return connection


async def disconnect_vercel_cli(user_id: str) -> None:
    await auth_store.delete_secret(user_id, VERCEL_SECRET)
    await auth_store.delete_connection(user_id, VERCEL_SECRET)


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
