import datetime
import json
import urllib.parse

import fastapi
import httpx
import pytest

import auth


def request(headers=None, cookies=None):
    return type(
        "Request",
        (),
        {
            "headers": headers or {"host": "localhost:3000"},
            "cookies": cookies or {},
            "url": type("URL", (), {"scheme": "http", "netloc": "localhost:3000"})(),
        },
    )()


async def test_save_user_is_compatible_with_existing_auth_schema(monkeypatch):
    class Database:
        def __init__(self):
            self.calls = []

        async def execute(self, query, *args):
            self.calls.append((query, args))

    database = Database()

    async def pool():
        return database

    monkeypatch.setattr(auth.auth_store.db, "pool", pool)
    user = {"id": "user_1", "name": "Ada"}

    assert await auth.auth_store.save_user(user) == user
    query, args = database.calls[0]
    assert "updated_at" not in query
    assert "hatchery_users.data || EXCLUDED.data" in query
    assert args == ("user_1", json.dumps(user))


def test_request_origin_uses_browser_facing_origin(monkeypatch):
    monkeypatch.setenv("HATCHERY_APP_ORIGIN", "https://hatchery.example/")

    assert auth.request_origin(request()) == "https://hatchery.example"


def test_origin_validation_accepts_direct_local_backend(monkeypatch):
    monkeypatch.delenv("HATCHERY_APP_ORIGIN", raising=False)
    local = request(headers={"host": "localhost:8000", "origin": "http://localhost:3000"})
    forged = request(headers={"host": "localhost:8000", "origin": "https://evil.example"})

    assert auth.valid_origin(local) is True
    assert auth.valid_origin(forged) is False


async def test_begin_uses_pkce_and_persists_one_time_state(monkeypatch):
    saved = {}

    async def save(state, data, lifetime):
        saved.update(state=state, data=data, lifetime=lifetime)

    monkeypatch.setattr(auth.auth_store, "save_oauth_state", save)

    response = await auth.begin(request())
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)

    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:3000/api/auth/callback"]
    assert saved["state"] == query["state"][0]
    assert saved["data"]["nonce"] == query["nonce"][0]
    assert saved["lifetime"] == datetime.timedelta(minutes=10)


async def test_verify_id_token_rejects_invalid_token(monkeypatch):
    class JWKS:
        def __init__(self, _url):
            pass

        def get_signing_key_from_jwt(self, _token):
            raise auth.jwt.InvalidTokenError("bad token")

    monkeypatch.setattr(auth.jwt, "PyJWKClient", JWKS)

    with pytest.raises(fastapi.HTTPException, match="invalid ID token"):
        await auth.verify_id_token("bad", "nonce")


async def test_callback_rejects_replayed_or_expired_state(monkeypatch):
    async def consume(_state):
        return None

    monkeypatch.setattr(auth.auth_store, "consume_oauth_state", consume)

    with pytest.raises(fastapi.HTTPException, match="invalid or expired OAuth state"):
        await auth.callback(request(), "code", "state")


async def test_callback_logs_safe_token_exchange_error(monkeypatch, caplog):
    async def consume(_state):
        return {"nonce": "nonce", "verifier": "private-verifier", "redirect_uri": "https://app/callback"}

    def responder(_request):
        return httpx.Response(
            400,
            json={
                "error": "invalid_client",
                "error_description": "Client authentication failed",
            },
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(auth.auth_store, "consume_oauth_state", consume)
    monkeypatch.setattr(auth.httpx, "AsyncClient", Client)

    with caplog.at_level("WARNING", logger="auth"):
        with pytest.raises(fastapi.HTTPException) as raised:
            await auth.callback(request(), "private-code", "state")

    assert raised.value.detail == (
        "Vercel token exchange failed: invalid_client: Client authentication failed"
    )
    assert "status=400 error=invalid_client description=Client authentication failed" in caplog.text
    assert "private-code" not in caplog.text
    assert "private-verifier" not in caplog.text
    assert "test-secret" not in caplog.text


async def test_callback_logs_nested_token_exchange_error(monkeypatch, caplog):
    async def consume(_state):
        return {"nonce": "nonce", "verifier": "verifier", "redirect_uri": "https://app/callback"}

    def responder(_request):
        return httpx.Response(400, json={"error": {"code": "invalid_grant", "message": "Code expired"}})

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(auth.auth_store, "consume_oauth_state", consume)
    monkeypatch.setattr(auth.httpx, "AsyncClient", Client)

    with caplog.at_level("WARNING", logger="auth"):
        with pytest.raises(fastapi.HTTPException) as raised:
            await auth.callback(request(), "code", "state")

    assert raised.value.detail == "Vercel token exchange failed: invalid_grant: Code expired"
    assert "error=invalid_grant description=Code expired" in caplog.text


async def test_callback_creates_session_without_storing_provider_tokens(monkeypatch):
    seen = {}

    async def consume(_state):
        return {"nonce": "nonce", "verifier": "verifier", "redirect_uri": "http://localhost/callback"}

    async def verify(_token, nonce):
        assert nonce == "nonce"
        return {"sub": "user_1", "email": "ada@example.com", "name": "Ada"}

    async def save_user(user):
        seen["user"] = user
        return user

    async def create_session(user_id, lifetime):
        seen["session"] = (user_id, lifetime)
        return "session-secret"

    def responder(http_request):
        assert http_request.url == auth.TOKEN_URL
        return httpx.Response(200, json={"id_token": "id", "access_token": "unused"})

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(auth.auth_store, "consume_oauth_state", consume)
    monkeypatch.setattr(auth, "verify_id_token", verify)
    monkeypatch.setattr(auth.auth_store, "save_user", save_user)
    monkeypatch.setattr(auth.auth_store, "create_session", create_session)
    monkeypatch.setattr(auth.httpx, "AsyncClient", Client)

    response = await auth.callback(request(), "code", "state")

    assert seen["user"]["id"] == "user_1"
    assert seen["session"] == ("user_1", datetime.timedelta(days=30))
    assert "session-secret" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "access_token" not in seen


async def test_github_authorization_uses_vercel_user_subject(monkeypatch):
    seen = {}

    async def start(connector, **kwargs):
        seen.update(connector=connector, **kwargs)
        return type("Authorization", (), {"url": "https://connect.vercel.com/authorize"})()

    monkeypatch.setenv("GITHUB_CONNECTOR", "github/hatchery")
    monkeypatch.setattr(auth.connect, "start_authorization", start)

    response = await auth.begin_github(request(), {"id": "user_1"})

    assert response.headers["location"] == "https://connect.vercel.com/authorize"
    assert seen["connector"] == "github/hatchery"
    assert seen["subject"] == auth.connect.ConnectUserTokenSubject(id="user_1")
    assert seen["return_url"] == "http://localhost:3000/api/connections/github/return"


async def test_github_return_saves_identity_without_token(monkeypatch):
    saved = {}

    async def token(_connector, **_kwargs):
        return type(
            "Token",
            (),
            {"token": "private-token", "installation_id": "inst_1"},
        )()

    async def save(user_id, connection):
        saved.update(user_id=user_id, connection=connection)

    def responder(http_request):
        assert http_request.headers["authorization"] == "Bearer private-token"
        return httpx.Response(
            200,
            json={
                "id": 42,
                "login": "octocat",
                "name": "The Octocat",
                "avatar_url": "https://github/avatar",
            },
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setenv("GITHUB_CONNECTOR", "github/hatchery")
    monkeypatch.setattr(auth.connect, "get_token_response", token)
    monkeypatch.setattr(auth.auth_store, "save_github_connection", save)
    monkeypatch.setattr(auth.httpx, "AsyncClient", Client)

    response = await auth.finish_github({"id": "user_1"})

    assert response.headers["location"] == "/"
    assert saved["user_id"] == "user_1"
    assert saved["connection"]["id"] == "42"
    assert saved["connection"]["login"] == "octocat"
    assert saved["connection"]["name"] == "The Octocat"
    assert saved["connection"]["installation_id"] == "inst_1"
    assert "token" not in saved["connection"]


async def test_github_token_uses_saved_installation(monkeypatch):
    seen = {}

    async def user(_user_id):
        return {"id": "user_1", "github": {"installation_id": "inst_1"}}

    async def token(connector, **kwargs):
        seen.update(connector=connector, **kwargs)
        return "private-token"

    monkeypatch.setenv("GITHUB_CONNECTOR", "github/hatchery")
    monkeypatch.setattr(auth.auth_store, "get_user", user)
    monkeypatch.setattr(auth.connect, "get_token", token)

    assert await auth.github_token("user_1") == "private-token"
    assert seen["connector"] == "github/hatchery"
    assert seen["subject"] == auth.connect.ConnectUserTokenSubject(id="user_1")
    assert seen["installation_id"] == "inst_1"


async def test_disconnect_github_revokes_grant_and_metadata(monkeypatch):
    seen = {}

    async def revoke(connector, **kwargs):
        seen.update(connector=connector, **kwargs)

    async def delete(user_id):
        seen["deleted"] = user_id

    monkeypatch.setenv("GITHUB_CONNECTOR", "github/hatchery")
    monkeypatch.setattr(auth.connect, "revoke_token", revoke)
    monkeypatch.setattr(auth.auth_store, "delete_github_connection", delete)

    await auth.disconnect_github(
        {"id": "user_1", "github": {"installation_id": "inst_1"}}
    )

    assert seen["connector"] == "github/hatchery"
    assert seen["subject"] == auth.connect.ConnectUserTokenSubject(id="user_1")
    assert seen["installation_id"] == "inst_1"
    assert seen["deleted"] == "user_1"


async def test_logout_deletes_server_session(monkeypatch):
    deleted = []

    async def delete(session_id):
        deleted.append(session_id)

    monkeypatch.setattr(auth.auth_store, "delete_session", delete)

    response = await auth.logout(request(cookies={auth.COOKIE: "session"}))

    assert response.status_code == 204
    assert deleted == ["session"]
    assert "Max-Age=0" in response.headers["set-cookie"]
