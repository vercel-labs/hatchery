import datetime

import fastapi
import httpx
import pytest

import auth
from store import auth as auth_store


def test_request_origin_uses_browser_facing_forwarded_headers():
    class Request:
        headers = {
            "host": "internal:41321",
            "x-forwarded-host": "preview.example",
            "x-forwarded-proto": "https",
        }
        url = type("URL", (), {"scheme": "http", "netloc": "internal:41321"})()

    assert auth.request_origin(Request()) == "https://preview.example"


def test_token_encryption_round_trip(monkeypatch):
    monkeypatch.setenv("HATCHERY_TOKEN_ENCRYPTION_KEY", "test-only-key")
    encrypted = auth.encrypt("secret")

    assert encrypted != "secret"
    assert auth.decrypt(encrypted) == "secret"


async def test_unexpired_access_token_does_not_refresh(monkeypatch):
    monkeypatch.setenv("HATCHERY_TOKEN_ENCRYPTION_KEY", "test-only-key")
    connection = {
        "user_id": "user_1",
        "access_token": auth.encrypt("access"),
        "refresh_token": auth.encrypt("refresh"),
        "access_expires_at": (auth_store.now() + datetime.timedelta(hours=1)).isoformat(),
    }

    token, updated = await auth._fresh_access_token(connection)

    assert token == "access"
    assert updated is None


async def test_vercel_get_preserves_api_error_message(monkeypatch):
    async def access_token(_user_id):
        return "access"

    def responder(_request):
        return httpx.Response(403, json={"error": {"message": "Missing required permission"}})

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(auth, "access_token", access_token)
    monkeypatch.setattr(auth.httpx, "AsyncClient", Client)

    with pytest.raises(fastapi.HTTPException, match="Missing required permission"):
        await auth.vercel_get("user_1", "/v2/teams")
