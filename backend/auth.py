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
from vercel import connect

from store import auth as auth_store

AUTHORIZE_URL = "https://vercel.com/oauth/authorize"
TOKEN_URL = "https://api.vercel.com/login/oauth/token"
JWKS_URL = "https://vercel.com/.well-known/jwks"
COOKIE = "hatchery_session"
SCOPES = "openid email profile"
GITHUB_API = "https://api.github.com"

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
    connection = user.get("github") if isinstance(user.get("github"), dict) else {}
    try:
        await connect.revoke_token(
            _github_connector(),
            subject=_github_subject(user),
            installation_id=connection.get("installation_id"),
        )
    except (connect.UserAuthorizationRequiredError, connect.NoValidTokenError):
        pass
    await auth_store.delete_github_connection(user["id"])


async def github_identity(user_id: str) -> dict | None:
    user = await auth_store.get_user(user_id)
    return github_connection(user or {})


async def github_token(user_id: str, installation_id: str | None = None) -> str:
    if installation_id is None:
        connection = await github_identity(user_id) or {}
        installation_id = connection.get("installation_id")
    return await connect.get_token(
        _github_connector(),
        subject=connect.ConnectUserTokenSubject(id=user_id),
        installation_id=installation_id,
    )


async def logout(request: fastapi.Request) -> fastapi.responses.Response:
    session_id = request.cookies.get(COOKIE)
    if session_id:
        await auth_store.delete_session(session_id)
    response = fastapi.responses.Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=_cookie_secure(request), samesite="lax")
    return response
