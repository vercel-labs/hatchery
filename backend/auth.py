"""Sign in with Vercel and Hatchery browser sessions."""

import base64
import datetime
import hashlib
import logging
import os
import secrets
import urllib.parse

import fastapi
import httpx
import jwt
from cryptography import fernet
from vercel import connect

from store import auth as auth_store

AUTHORIZE_URL = "https://vercel.com/oauth/authorize"
TOKEN_URL = "https://api.vercel.com/login/oauth/token"
JWKS_URL = "https://vercel.com/.well-known/jwks"
COOKIE = "hatchery_session"
SCOPES = "openid email profile"
GITHUB_API = "https://api.github.com"
VERCEL_API = "https://api.vercel.com"
VERCEL_SECRET = "vercel_cli"

log = logging.getLogger("auth")


def _client_id() -> str:
    return os.environ["VERCEL_APP_CLIENT_ID"]


def _client_secret() -> str:
    return os.environ["VERCEL_APP_CLIENT_SECRET"]


def request_origin(request: fastapi.Request) -> str:
    configured = os.environ.get("HATCHERY_APP_ORIGIN")
    if configured:
        return configured.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def _cookie_secure(request: fastapi.Request) -> bool:
    return request_origin(request).startswith("https://")


def valid_origin(request: fastapi.Request | fastapi.WebSocket) -> bool:
    origin = getattr(request, "headers", {}).get("origin")
    if not origin:
        return True
    configured = os.environ.get("HATCHERY_APP_ORIGIN")
    expected = configured.rstrip("/") if configured else request_origin(request)
    supplied = origin.rstrip("/")
    if secrets.compare_digest(supplied, expected):
        return True
    return supplied in {"http://localhost:3000", "http://127.0.0.1:3000"} and expected in {
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }


async def begin(request: fastapi.Request) -> fastapi.responses.RedirectResponse:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = f"{request_origin(request)}/api/auth/callback"
    await auth_store.save_oauth_state(
        state,
        {"nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri},
        datetime.timedelta(minutes=10),
    )
    query = urllib.parse.urlencode(
        {
            "client_id": _client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return fastapi.responses.RedirectResponse(f"{AUTHORIZE_URL}?{query}")


async def callback(
    request: fastapi.Request, code: str, state: str
) -> fastapi.responses.RedirectResponse:
    pending = await auth_store.consume_oauth_state(state)
    if pending is None:
        raise fastapi.HTTPException(400, "invalid or expired OAuth state")
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending["redirect_uri"],
                "code_verifier": pending["verifier"],
            },
            auth=(_client_id(), _client_secret()),
        )
    if response.status_code >= 400:
        error_code = "unknown_error"
        error_description = "Vercel rejected the token exchange"
        try:
            payload = response.json()
            if isinstance(payload, dict):
                raw_error = payload.get("error")
                if isinstance(raw_error, dict):
                    error_code = str(raw_error.get("code") or error_code)
                    error_description = str(raw_error.get("message") or error_description)
                elif raw_error:
                    error_code = str(raw_error)
                error_description = str(
                    payload.get("error_description") or payload.get("message") or error_description
                )
        except ValueError:
            pass
        log.warning(
            "Vercel token exchange failed: status=%s error=%s description=%s",
            response.status_code,
            error_code,
            error_description,
        )
        raise fastapi.HTTPException(
            502, f"Vercel token exchange failed: {error_code}: {error_description}"
        )
    tokens = response.json()
    claims = await verify_id_token(tokens["id_token"], pending["nonce"])
    user = await auth_store.save_user(
        {
            "id": claims["sub"],
            "email": claims.get("email"),
            "name": claims.get("name"),
            "username": claims.get("preferred_username"),
            "picture": claims.get("picture"),
        }
    )
    session_id = await auth_store.create_session(user["id"], datetime.timedelta(days=30))
    response = fastapi.responses.RedirectResponse("/")
    response.set_cookie(
        COOKIE,
        session_id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


async def verify_id_token(token: str, nonce: str) -> dict:
    import asyncio

    try:
        jwks = jwt.PyJWKClient(JWKS_URL)
        key = await asyncio.to_thread(jwks.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=_client_id(),
            issuer="https://vercel.com",
        )
    except jwt.PyJWTError as error:
        raise fastapi.HTTPException(400, "invalid ID token") from error
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise fastapi.HTTPException(400, "invalid ID token nonce")
    return claims


async def session_user(session_id: str) -> dict | None:
    return await auth_store.session_user(session_id) if session_id else None


async def current_user(request: fastapi.Request) -> dict | None:
    return await session_user(request.cookies.get(COOKIE, ""))


def _github_connector() -> str:
    return os.environ["GITHUB_CONNECTOR"]


def _github_subject(user: dict) -> connect.ConnectUserTokenSubject:
    return connect.ConnectUserTokenSubject(id=user["id"])


def github_connection(user: dict) -> dict | None:
    saved = user.get("github")
    return saved if isinstance(saved, dict) else None


async def begin_github(request: fastapi.Request, user: dict) -> fastapi.responses.RedirectResponse:
    authorization = await connect.start_authorization(
        _github_connector(),
        subject=_github_subject(user),
        return_url=f"{request_origin(request)}/api/connections/github/return",
    )
    return fastapi.responses.RedirectResponse(authorization.url)


async def finish_github(user: dict) -> fastapi.responses.RedirectResponse:
    token = await connect.get_token_response(
        _github_connector(), subject=_github_subject(user)
    )
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
    previous = github_connection(user) or {}
    connection = {
        "id": str(profile["id"]),
        "login": profile["login"],
        "name": profile.get("name"),
        "avatar_url": profile.get("avatar_url"),
        "installation_id": token.installation_id,
        "installations": previous.get("installations", {}),
        "connected_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    await auth_store.save_github_connection(user["id"], connection)
    return fastapi.responses.RedirectResponse("/")


async def disconnect_github(user: dict) -> None:
    connection = user.get("github") if isinstance(user.get("github"), dict) else {}
    mapped = connection.get("installations")
    installation_ids = {
        str(value)
        for value in (mapped.values() if isinstance(mapped, dict) else ())
        if value
    }
    if connection.get("installation_id"):
        installation_ids.add(str(connection["installation_id"]))
    for installation_id in installation_ids or {""}:
        try:
            await connect.revoke_token(
                _github_connector(),
                subject=_github_subject(user),
                installation_id=installation_id or None,
            )
        except (connect.UserAuthorizationRequiredError, connect.NoValidTokenError):
            pass
        except Exception:
            log.exception(
                "GitHub token revocation failed",
                extra={"user_id": user["id"], "installation_id": installation_id},
            )
    await auth_store.delete_github_connection(user["id"])


async def github_identity(user_id: str) -> dict | None:
    user = await auth_store.get_user(user_id)
    return github_connection(user or {})


async def github_installation_id(user_id: str, owner: str, repo: str) -> str:
    connection = await github_identity(user_id)
    if connection is None:
        raise RuntimeError("connect GitHub before accessing repositories")
    key = owner.lower()
    installations = connection.get("installations")
    if isinstance(installations, dict) and installations.get(key):
        return str(installations[key])
    try:
        token = await connect.get_token_response(
            _github_connector(),
            subject=connect.ConnectUserTokenSubject(id=user_id),
            authorization_details=[
                connect.ConnectGitHubAppInstallationAuthorizationDetail(
                    org=owner, repositories=[repo]
                )
            ],
        )
    except Exception:
        log.exception(
            "GitHub installation resolution failed",
            extra={"user_id": user_id, "github_owner": owner, "github_repo": repo},
        )
        raise
    installation_id = str(token.installation_id or "")
    if not installation_id:
        log.error(
            "GitHub installation resolution returned no installation",
            extra={"user_id": user_id, "github_owner": owner, "github_repo": repo},
        )
        raise RuntimeError(f"Hatchery GitHub app is not installed for {owner}")
    await auth_store.save_github_installation(user_id, key, installation_id)
    log.info(
        "GitHub installation resolved",
        extra={
            "user_id": user_id,
            "github_owner": owner,
            "github_repo": repo,
            "installation_id": installation_id,
        },
    )
    return installation_id


async def github_token(
    user_id: str,
    installation_id: str | None = None,
    *,
    repo: str | None = None,
) -> str:
    if repo:
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name:
            raise ValueError("invalid GitHub repository")
        installation_id = await github_installation_id(user_id, owner, name)
    elif installation_id is None:
        connection = await github_identity(user_id) or {}
        installation_id = connection.get("installation_id")
    return await connect.get_token(
        _github_connector(),
        subject=connect.ConnectUserTokenSubject(id=user_id),
        installation_id=installation_id,
    )


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
    async with httpx.AsyncClient(
        base_url=VERCEL_API, headers=headers, timeout=30
    ) as http:
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


async def logout(request: fastapi.Request) -> fastapi.responses.Response:
    session_id = request.cookies.get(COOKIE)
    if session_id:
        await auth_store.delete_session(session_id)
    response = fastapi.responses.Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=_cookie_secure(request), samesite="lax")
    return response
