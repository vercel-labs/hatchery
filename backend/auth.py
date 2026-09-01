"""Sign in with Vercel and Hatchery browser sessions."""

import base64
import datetime
import hashlib
import os
import secrets
import urllib.parse

import fastapi
import httpx
import jwt

from store import auth as auth_store

AUTHORIZE_URL = "https://vercel.com/oauth/authorize"
TOKEN_URL = "https://api.vercel.com/login/oauth/token"
JWKS_URL = "https://vercel.com/.well-known/jwks"
COOKIE = "hatchery_session"
SCOPES = "openid email profile"


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
        raise fastapi.HTTPException(502, "Vercel token exchange failed")
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


async def logout(request: fastapi.Request) -> fastapi.responses.Response:
    session_id = request.cookies.get(COOKIE)
    if session_id:
        await auth_store.delete_session(session_id)
    response = fastapi.responses.Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=_cookie_secure(request), samesite="lax")
    return response
