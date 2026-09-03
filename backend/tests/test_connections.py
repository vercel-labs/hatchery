import httpx
import pytest

import connections


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


async def test_vercel_cli_token_is_validated_and_encrypted(monkeypatch):
    saved = {}
    monkeypatch.setenv(
        "HATCHERY_CREDENTIAL_KEY", connections.fernet.Fernet.generate_key().decode()
    )

    def responder(request):
        assert request.headers["authorization"] == "Bearer private-token"
        return httpx.Response(
            200,
            json={"user": {"id": "vercel_1", "username": "ada", "email": "ada@example.com"}},
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    async def save_secret(user_id, name, value):
        saved["secret"] = (user_id, name, value)

    async def save_connection(user_id, name, connection):
        saved["connection"] = (user_id, name, connection)

    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)
    monkeypatch.setattr(connections.auth_store, "save_secret", save_secret)
    monkeypatch.setattr(connections.auth_store, "save_connection", save_connection)

    connection = await connections.connect_vercel_cli("user_1", " private-token ")

    assert connection["user_id"] == "vercel_1"
    assert "private-token" not in saved["secret"][2]
    encrypted = saved["secret"][2]

    async def get_secret(_user_id, _name):
        return encrypted

    monkeypatch.setattr(connections.auth_store, "get_secret", get_secret)
    assert await connections.vercel_cli_token("user_1") == "private-token"


async def test_vercel_cli_rejects_invalid_token_without_storing(monkeypatch):
    def responder(_request):
        return httpx.Response(401, json={"error": {"message": "bad token"}})

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)

    with pytest.raises(ValueError, match="Vercel rejected"):
        await connections.connect_vercel_cli("user_1", "bad")


async def test_github_authorization_uses_vercel_user_subject(monkeypatch):
    seen = {}

    async def start(connector, **kwargs):
        seen.update(connector=connector, **kwargs)
        return type("Authorization", (), {"url": "https://connect.vercel.com/authorize"})()

    monkeypatch.setenv("GITHUB_CONNECTOR", "github/hatchery")
    monkeypatch.setattr(connections.connect, "start_authorization", start)

    response = await connections.begin_github(request(), {"id": "user_1"})

    assert response.headers["location"] == "https://connect.vercel.com/authorize"
    assert seen["connector"] == "github/hatchery"
    assert seen["subject"] == connections.connect.ConnectUserTokenSubject(id="user_1")
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
    monkeypatch.setattr(connections.connect, "get_token_response", token)
    monkeypatch.setattr(connections.auth_store, "save_github_connection", save)
    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)

    response = await connections.finish_github({"id": "user_1"})

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
    monkeypatch.setattr(connections.auth_store, "get_user", user)
    monkeypatch.setattr(connections.connect, "get_token", token)

    assert await connections.github_token("user_1") == "private-token"
    assert seen["connector"] == "github/hatchery"
    assert seen["subject"] == connections.connect.ConnectUserTokenSubject(id="user_1")
    assert seen["installation_id"] == "inst_1"


async def test_github_repo_warning_reports_missing_organization_installation(monkeypatch):
    async def identity(_user_id):
        return {"installation_id": "inst_personal"}

    async def token(_user_id, _installation_id=None):
        return "private-token"

    def responder(request):
        if request.url.path == "/repos/old-owner/app":
            return httpx.Response(200, json={"full_name": "acme/app"})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 1,
                            "account": {"login": "octocat"},
                            "permissions": {
                                "contents": "write",
                                "pull_requests": "write",
                            },
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(connections, "github_identity", identity)
    monkeypatch.setattr(connections, "github_token", token)
    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)

    assert await connections.github_repo_warning("user_1", "old-owner/app") == (
        "Install the Hatchery GitHub app on acme to make pull requests to acme/app."
    )


async def test_github_repo_warning_accepts_writable_selected_repository(monkeypatch):
    async def identity(_user_id):
        return {"installation_id": "inst_acme"}

    async def token(_user_id, _installation_id=None):
        return "private-token"

    def responder(request):
        if request.url.path == "/repos/acme/app":
            return httpx.Response(200, json={"full_name": "acme/app"})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 42,
                            "account": {"login": "acme"},
                            "permissions": {
                                "contents": "write",
                                "pull_requests": "write",
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/user/installations/42/repositories":
            return httpx.Response(
                200, json={"repositories": [{"full_name": "acme/app"}]}
            )
        raise AssertionError(request.url)

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(connections, "github_identity", identity)
    monkeypatch.setattr(connections, "github_token", token)
    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)

    assert await connections.github_repo_warning("user_1", "acme/app") is None


async def test_disconnect_github_revokes_grant_and_metadata(monkeypatch):
    seen = {}

    async def revoke(connector, **kwargs):
        seen.update(connector=connector, **kwargs)

    async def delete(user_id):
        seen["deleted"] = user_id

    monkeypatch.setenv("GITHUB_CONNECTOR", "github/hatchery")
    monkeypatch.setattr(connections.connect, "revoke_token", revoke)
    monkeypatch.setattr(connections.auth_store, "delete_github_connection", delete)

    await connections.disconnect_github(
        {"id": "user_1", "github": {"installation_id": "inst_1"}}
    )

    assert seen["connector"] == "github/hatchery"
    assert seen["subject"] == connections.connect.ConnectUserTokenSubject(id="user_1")
    assert seen["installation_id"] == "inst_1"
    assert seen["deleted"] == "user_1"


async def test_slack_authorization_uses_vercel_user_subject(monkeypatch):
    seen = {}

    async def start(connector, **kwargs):
        seen.update(connector=connector, **kwargs)
        return type("Authorization", (), {"url": "https://connect.vercel.com/authorize"})()

    monkeypatch.setenv("SLACK_CONNECTOR", "slack/hatchery")
    monkeypatch.setattr(connections.connect, "start_authorization", start)

    response = await connections.begin_slack(request(), {"id": "user_1"})

    assert response.headers["location"] == "https://connect.vercel.com/authorize"
    assert seen["connector"] == "slack/hatchery"
    assert seen["subject"] == connections.connect.ConnectUserTokenSubject(id="user_1")
    assert seen["return_url"] == "http://localhost:3000/api/connections/slack/return"


async def test_slack_return_uses_connect_identity_and_saves_no_token(monkeypatch):
    saved = {}

    async def token(_connector, **kwargs):
        assert kwargs["subject"] == connections.connect.ConnectUserTokenSubject(id="user_1")
        return type(
            "Token",
            (),
            {
                "token": "xoxp-private",
                "tenant_id": "T1",
                "external_subject": "U1",
                "metadata": {"team_name": "Acme", "user_name": "ada"},
                "name": None,
                "token_id": "stk_1",
            },
        )()

    async def save(user_id, connection):
        saved.update(user_id=user_id, connection=connection)

    class Client:
        def __init__(self, **_kwargs):
            raise AssertionError("auth.test should not be called when Connect returns identity")

    monkeypatch.setenv("SLACK_CONNECTOR", "slack/hatchery")
    monkeypatch.setattr(connections.connect, "get_token_response", token)
    monkeypatch.setattr(connections.auth_store, "save_slack_connection", save)
    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)

    response = await connections.finish_slack({"id": "user_1"})

    assert response.headers["location"] == "/"
    assert saved["user_id"] == "user_1"
    assert saved["connection"] | {"connected_at": "ignored"} == {
        "team_id": "T1",
        "team": "Acme",
        "user_id": "U1",
        "user": "ada",
        "connected_at": "ignored",
    }
    assert "token" not in saved["connection"]


async def test_slack_return_rejects_identity_linked_to_another_user(monkeypatch):
    async def token(_connector, **_kwargs):
        return type(
            "Token",
            (),
            {
                "token": "xoxp-private",
                "tenant_id": None,
                "external_subject": None,
                "metadata": None,
                "name": None,
                "token_id": "stk_1",
            },
        )()

    async def save(_user_id, _connection):
        raise connections.auth_store.SlackIdentityConflict("already linked")

    def responder(_request):
        return httpx.Response(
            200,
            json={"ok": True, "team_id": "T1", "user_id": "U1"},
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setenv("SLACK_CONNECTOR", "slack/hatchery")
    monkeypatch.setattr(connections.connect, "get_token_response", token)
    monkeypatch.setattr(connections.auth_store, "save_slack_connection", save)
    monkeypatch.setattr(connections.httpx, "AsyncClient", Client)

    with pytest.raises(connections.fastapi.HTTPException) as error:
        await connections.finish_slack({"id": "user_2"})

    assert error.value.status_code == 409


async def test_disconnect_slack_revokes_grant_and_mapping(monkeypatch):
    seen = {}

    async def revoke(connector, **kwargs):
        seen.update(connector=connector, **kwargs)

    async def delete(user_id):
        seen["deleted"] = user_id

    monkeypatch.setenv("SLACK_CONNECTOR", "slack/hatchery")
    monkeypatch.setattr(connections.connect, "revoke_token", revoke)
    monkeypatch.setattr(connections.auth_store, "delete_slack_connection", delete)

    await connections.disconnect_slack({"id": "user_1"})

    assert seen == {
        "connector": "slack/hatchery",
        "subject": connections.connect.ConnectUserTokenSubject(id="user_1"),
        "deleted": "user_1",
    }
