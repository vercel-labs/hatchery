"""Storage-repo credentials from Vercel Connect, installed per Git operation."""

import base64
import pathlib
import typing

import pytest
import vercel.connect

from hatchery.workspace import connect as workspace_connect
from hatchery.workspace import git as workspace_git
from hatchery.workspace import repo as workspace_repo


async def test_connect_requests_a_repository_scoped_github_app_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, typing.Any]]] = []

    async def get_token(connector: str, **options: typing.Any) -> str:
        requests.append((connector, options))
        return "short-lived-github-token"

    monkeypatch.setattr(vercel.connect, "get_token", get_token)
    credential = workspace_connect.ConnectGitHubToken(
        "github/acme-hatchery", installation_id="inst_acme"
    ).for_remote("https://github.com/acme/workspace.git")

    assert await credential() == "short-lived-github-token"
    connector, options = requests[0]
    assert connector == "github/acme-hatchery"
    assert options["subject"].model_dump() == {"type": "app"}
    assert options["installation_id"] == "inst_acme"
    assert options["authorization_details"][0].model_dump() == {
        "org": "acme",
        "permissions": ("contents:write", "pull_requests:write"),
        "repositories": ("workspace",),
        "type": "github_app_installation",
    }
    assert "options" not in options


async def test_connect_is_preferred_with_static_github_token_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = "https://github.com/acme/workspace.git"
    monkeypatch.setenv("GITHUB_TOKEN", "static-token")
    monkeypatch.setenv("GITHUB_CONNECTOR", "github/acme-hatchery")
    monkeypatch.setenv("HATCHERY_GITHUB_INSTALLATION_ID", "inst_acme")
    installations: list[str | None] = []

    async def get_token(connector: str, **options: typing.Any) -> str:
        installations.append(options["installation_id"])
        return "connect-token"

    monkeypatch.setattr(vercel.connect, "get_token", get_token)

    connected = workspace_connect.github_credentials(remote)
    assert callable(connected)
    assert await connected() == "connect-token"
    assert installations == ["inst_acme"]

    monkeypatch.delenv("GITHUB_CONNECTOR")
    assert workspace_connect.github_credentials(remote) == "static-token"


def test_github_environment_is_ignored_for_a_local_remote(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_CONNECTOR", "github/acme-hatchery")
    monkeypatch.setenv("GITHUB_TOKEN", "static-token")

    assert workspace_connect.github_credentials(tmp_path.as_uri()) is None


def test_storage_remote_comes_from_the_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCHERY_STORAGE_REPO", "vercel-internal-playground/hatchery-storage")
    assert workspace_connect.storage_remote() == (
        "https://github.com/vercel-internal-playground/hatchery-storage.git"
    )

    monkeypatch.setenv("HATCHERY_STORAGE_REPO", tmp_path.as_uri())
    assert workspace_connect.storage_remote() == tmp_path.as_uri()

    monkeypatch.setenv("HATCHERY_STORAGE_REPO", "https://token@github.com/acme/storage")
    with pytest.raises(ValueError, match="credentials"):
        workspace_connect.storage_remote()


async def test_workspace_resolves_renewable_credentials_for_each_operation() -> None:
    issued = iter(("first-token", "second-token"))

    async def credential() -> str:
        return next(issued)

    repo = workspace_repo.WorkspaceRepo("https://github.com/acme/workspace.git", token=credential)

    assert repo.has_credentials
    assert await repo.credential() == "first-token"
    assert await repo.credential() == "second-token"


def auth_headers(env: dict[str, str]) -> list[str]:
    """The decoded GitHub Authorization headers a Git child would receive."""
    headers = []
    for index in range(int(env["GIT_CONFIG_COUNT"])):
        if env[f"GIT_CONFIG_KEY_{index}"] == "http.https://github.com/.extraHeader":
            value = env[f"GIT_CONFIG_VALUE_{index}"]
            assert value.startswith("Authorization: Basic ")
            headers.append(base64.b64decode(value.removeprefix("Authorization: Basic ")).decode())
    return headers


def test_reused_git_session_installs_only_the_current_token() -> None:
    with workspace_git.Git("https://github.com/acme/workspace.git", token="first-token") as git:
        assert auth_headers(git.env) == ["x-access-token:first-token"]

        git.set_token("second-token")
        assert auth_headers(git.env) == ["x-access-token:second-token"]
        stale = base64.b64encode(b"x-access-token:first-token").decode()
        assert not any("first-token" in value or stale in value for value in git.env.values())

        git.set_token(None)
        assert auth_headers(git.env) == []
        count = int(git.env["GIT_CONFIG_COUNT"])
        assert f"GIT_CONFIG_KEY_{count}" not in git.env, "stale trailing entries were not removed"


def test_git_session_ignores_a_token_for_a_local_remote(tmp_path: pathlib.Path) -> None:
    with workspace_git.Git(tmp_path.as_uri(), token="never-sent") as git:
        assert auth_headers(git.env) == []
        assert not any("never-sent" in value for value in git.env.values())


async def test_workspace_rejects_an_empty_renewed_token() -> None:
    async def credential() -> str:
        return ""

    repo = workspace_repo.WorkspaceRepo("https://github.com/acme/workspace.git", token=credential)
    with pytest.raises(ValueError, match="token"):
        await repo.credential()
