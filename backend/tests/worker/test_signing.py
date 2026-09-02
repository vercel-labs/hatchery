import base64
import json

import httpx
import pytest

from worker import signing


async def test_sign_commits_uses_local_changes_with_github_signed_graphql(monkeypatch):
    seen = {}

    async def token(connector, **kwargs):
        seen.update(connector=connector, **kwargs)
        return type("Token", (), {"token": "app-token"})()

    def responder(request):
        assert request.url.path == "/graphql"
        assert request.headers["authorization"].startswith("Bearer ")
        commit = json.loads(request.read())["variables"]["input"]
        assert commit["branch"]["branchName"] == "hatchery/sign-1"
        assert commit["expectedHeadOid"] == "a" * 40
        assert commit["message"]["body"].endswith(
            "Co-Authored-By: A <a@example.com>"
        )
        assert commit["fileChanges"] == {
            "additions": [
                {
                    "path": "new.txt",
                    "contents": base64.b64encode(b"new content").decode(),
                }
            ],
            "deletions": [{"path": "old.txt"}],
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "createCommitOnBranch": {
                        "commit": {"oid": "c" * 40, "signature": {"isValid": True}}
                    }
                }
            },
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(signing.connect, "get_token_response", token)
    monkeypatch.setattr(signing.httpx, "AsyncClient", Client)

    result = await signing.sign_commits(
        "github/hatchery",
        "inst_acme",
        {
            "repo": {"owner": "acme", "name": "app"},
            "branch": "hatchery/sign-1",
            "base_oid": "a" * 40,
            "commits": [
                {
                    "sha": "b" * 40,
                    "message": "change",
                    "original_author": {"name": "A", "email": "a@example.com"},
                    "file_changes": {
                        "additions": [
                            {
                                "path": "new.txt",
                                "contents": base64.b64encode(b"new content").decode(),
                            }
                        ],
                        "deletions": [{"path": "old.txt"}],
                    },
                }
            ],
        },
    )

    assert result == ["c" * 40]
    assert seen == {
        "connector": "github/hatchery",
        "subject": signing.connect.ConnectAppTokenSubject(),
        "installation_id": "inst_acme",
    }


async def test_sign_commits_logs_app_token_failure(monkeypatch, caplog):
    async def token(_connector, **_kwargs):
        raise RuntimeError("mint failed")

    monkeypatch.setattr(signing.connect, "get_token_response", token)

    with caplog.at_level("ERROR", logger="worker.signing"):
        with pytest.raises(RuntimeError, match="mint failed"):
            await signing.sign_commits(
                "github/hatchery",
                "inst_acme",
                {
                    "repo": {"owner": "acme", "name": "app"},
                    "branch": "hatchery/sign-1",
                    "base_oid": "a" * 40,
                    "commits": [],
                },
            )

    assert "GitHub app token mint failed" in caplog.text


async def test_sign_commits_rejects_unsigned_graphql_result(monkeypatch):
    async def token(_connector, **_kwargs):
        return type("Token", (), {"token": "app-token"})()

    def responder(_request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "createCommitOnBranch": {
                        "commit": {"oid": "c" * 40, "signature": None}
                    }
                }
            },
        )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(responder), **kwargs)

    monkeypatch.setattr(signing.connect, "get_token_response", token)
    monkeypatch.setattr(signing.httpx, "AsyncClient", Client)

    with pytest.raises(RuntimeError, match="without a valid signature"):
        await signing.sign_commits(
            "github/hatchery",
            "inst_acme",
            {
                "repo": {"owner": "acme", "name": "app"},
                "branch": "hatchery/sign-1",
                "base_oid": "a" * 40,
                "commits": [
                    {
                        "message": "change",
                        "file_changes": {
                            "additions": [
                                {
                                    "path": "new.txt",
                                    "contents": base64.b64encode(b"new").decode(),
                                }
                            ]
                        },
                    }
                ],
            },
        )
