"""Sign in with Vercel and delegated Vercel API access."""

import base64
import datetime
import hashlib
import json
import os
import secrets
import urllib.parse

import cryptography.fernet
import fastapi
import httpx

import store
from store import auth

AUTHORIZE_URL = "https://vercel.com/oauth/authorize"
TOKEN_URL = "https://api.vercel.com/login/oauth/token"
REVOKE_URL = "https://api.vercel.com/login/oauth/token/revoke"
JWKS_URL = "https://vercel.com/.well-known/jwks"
API = "https://api.vercel.com"
COOKIE = "hatchery_session"
SCOPES = "openid email profile offline_access"


def _client_id() -> str:
    return os.environ["VERCEL_APP_CLIENT_ID"]


def _client_secret() -> str:
    return os.environ["VERCEL_APP_CLIENT_SECRET"]


def _fernet() -> cryptography.fernet.Fernet:
    raw = os.environ["HATCHERY_TOKEN_ENCRYPTION_KEY"].encode()
    try:
        return cryptography.fernet.Fernet(raw)
    except ValueError:
        return cryptography.fernet.Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        )


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def request_origin(request: fastapi.Request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or request.url.scheme
    return f"{scheme}://{host}"


def cookie_secure(request: fastapi.Request) -> bool:
    return request_origin(request).startswith("https://")


async def begin(request: fastapi.Request) -> fastapi.responses.RedirectResponse:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = f"{request_origin(request)}/api/auth/callback"
    await auth.save_oauth_state(
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
    pending = await auth.consume_oauth_state(state)
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
        raise fastapi.HTTPException(502, "Vercel token exchange failed")
    tokens = response.json()
    claims = await verify_id_token(tokens["id_token"], pending["nonce"])
    user = await auth.save_user(
        {
            "id": claims["sub"],
            "email": claims.get("email"),
            "name": claims.get("name"),
            "username": claims.get("preferred_username"),
            "picture": claims.get("picture"),
        }
    )
    expires_at = auth.now() + datetime.timedelta(seconds=int(tokens.get("expires_in", 3600)))
    await auth.save_connection(
        {
            "user_id": user["id"],
            "access_token": encrypt(tokens["access_token"]),
            "refresh_token": encrypt(tokens["refresh_token"]),
            "access_expires_at": expires_at.isoformat(),
        }
    )
    session_id = await auth.create_session(user["id"], datetime.timedelta(days=30))
    response = fastapi.responses.RedirectResponse("/")
    response.set_cookie(
        COOKIE,
        session_id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


async def verify_id_token(token: str, nonce: str) -> dict:
    import jwt

    jwks = jwt.PyJWKClient(JWKS_URL)
    key = await __import__("asyncio").to_thread(jwks.get_signing_key_from_jwt, token)
    claims = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=_client_id(),
        issuer="https://vercel.com",
    )
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise fastapi.HTTPException(400, "invalid ID token nonce")
    return claims


async def current_user(request: fastapi.Request) -> dict | None:
    session_id = request.cookies.get(COOKIE)
    return await auth.session_user(session_id) if session_id else None


async def require_user(request: fastapi.Request) -> dict:
    user = await current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    return user


async def logout(request: fastapi.Request) -> fastapi.responses.Response:
    session_id = request.cookies.get(COOKIE)
    user = await current_user(request)
    if user is not None:
        connection = await auth.get_connection(user["id"])
        if connection is not None:
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    await http.post(
                        REVOKE_URL,
                        data={"token": decrypt(connection["refresh_token"])},
                        auth=(_client_id(), _client_secret()),
                    )
            except httpx.HTTPError:
                pass
    if session_id:
        await auth.delete_session(session_id)
    response = fastapi.responses.Response(status_code=204)
    response.delete_cookie(COOKIE, path="/")
    return response


async def access_token(user_id: str) -> str:
    async with auth.refresh_lock(user_id):
        if store.use_postgres():
            from store import db

            pool = await db.pool()
            async with pool.acquire() as database, database.transaction():
                row = await database.fetchrow(
                    "SELECT data FROM hatchery_vercel_connections WHERE user_id = $1 FOR UPDATE",
                    user_id,
                )
                connection = auth._data(row["data"]) if row else None
                token = await _fresh_access_token(connection)
                if token[1] is not None:
                    await database.execute(
                        "UPDATE hatchery_vercel_connections SET data = $2::jsonb, updated_at = now() "
                        "WHERE user_id = $1",
                        user_id,
                        json.dumps(token[1]),
                    )
                return token[0]
        connection = await auth.get_connection(user_id)
        token, updated = await _fresh_access_token(connection)
        if updated is not None:
            await auth.save_connection(updated)
        return token


async def _fresh_access_token(connection: dict | None) -> tuple[str, dict | None]:
    if connection is None:
        raise fastapi.HTTPException(409, "Vercel connection required")
    if connection["access_expires_at"] > (
        auth.now() + datetime.timedelta(minutes=5)
    ).isoformat():
        return decrypt(connection["access_token"]), None
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": decrypt(connection["refresh_token"]),
            },
            auth=(_client_id(), _client_secret()),
        )
    if response.status_code >= 400:
        raise fastapi.HTTPException(401, "Vercel connection expired; sign in again")
    tokens = response.json()
    connection.update(
        access_token=encrypt(tokens["access_token"]),
        refresh_token=encrypt(tokens["refresh_token"]),
        access_expires_at=(
            auth.now() + datetime.timedelta(seconds=int(tokens.get("expires_in", 3600)))
        ).isoformat(),
    )
    return tokens["access_token"], connection


async def vercel_get(user_id: str, path: str, **params) -> dict:
    token = await access_token(user_id)
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(
            f"{API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        try:
            error = response.json().get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else None
        except ValueError:
            detail = None
        raise fastapi.HTTPException(
            response.status_code,
            detail or f"Vercel API request failed ({response.status_code})",
        )
    return response.json()


async def teams(user_id: str) -> list[dict]:
    payload = await vercel_get(user_id, "/v2/teams", limit=100)
    return [
        {"id": team["id"], "name": team.get("name") or team.get("slug"), "slug": team.get("slug")}
        for team in payload.get("teams", [])
    ]


async def projects(user_id: str, team_id: str) -> list[dict]:
    payload = await vercel_get(user_id, "/v9/projects", teamId=team_id, limit=100)
    return [
        {
            "id": project["id"],
            "name": project["name"],
            "link": project.get("link"),
        }
        for project in payload.get("projects", [])
    ]


def github_repo(project: dict) -> str | None:
    link = project.get("link") or {}
    if link.get("type") not in ("github", "github-limited"):
        return None
    if not link.get("org") or not link.get("repo"):
        return None
    return f"{link['org']}/{link['repo']}"
