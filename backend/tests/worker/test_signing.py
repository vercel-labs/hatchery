import httpx
import pytest

from worker import signing


async def test_sign_commits_resolves_installation_for_repository(monkeypatch):
    seen = {}

    async def token(connector, **kwargs):
        seen.update(connector=connector, **kwargs)
        return type("Token", (), {"token": "app-token", "installation_id": "inst_acme"})()

    def responder(request):
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.url.path == "/repos/acme/app/git/commits"
        assert request.read()
        return httpx.Response(201, json={"sha": "signed"})

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(signing.connect, "get_token_response", token)
    monkeypatch.setattr(signing.httpx, "AsyncClient", Client)

    result = await signing.sign_commits(
        "github/hatchery",
        {
            "repo": {"owner": "acme", "name": "app"},
            "commits": [
                {
                    "message": "change",
                    "tree_sha": "tree",
                    "parents": ["parent"],
                }
            ],
        },
    )

    assert result == ["signed"]
    assert seen == {
        "connector": "github/hatchery",
        "subject": signing.connect.ConnectAppTokenSubject(),
        "authorization_details": [
            signing.connect.ConnectGitHubAppInstallationAuthorizationDetail(
                org="acme", repositories=["app"]
            )
        ],
    }


async def test_sign_commits_surfaces_github_permission_detail(monkeypatch):
    async def token(_connector, **_kwargs):
        return type("Token", (), {"token": "app-token"})()

    def responder(_request):
        return httpx.Response(
            403,
            json={"message": "Resource not accessible by integration"},
            headers={"x-accepted-github-permissions": "contents=write"},
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(signing.connect, "get_token_response", token)
    monkeypatch.setattr(signing.httpx, "AsyncClient", Client)

    with pytest.raises(RuntimeError, match="contents=write"):
        await signing.sign_commits(
            "github/hatchery",
            {
                "repo": {"owner": "acme", "name": "app"},
                "commits": [
                    {
                        "message": "change",
                        "tree_sha": "tree",
                        "parents": ["parent"],
                    }
                ],
            },
        )
