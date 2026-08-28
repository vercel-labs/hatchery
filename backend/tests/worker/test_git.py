import json
import subprocess

from worker import git
from worker.daemon import main


def completed(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_git_argument_parser_composes_directories_and_finds_subcommand():
    parsed = git.parse_git_args(["-c", "x=y", "-C", "one", "-C", "two", "push", "origin"])
    assert parsed.subcommand == "push"
    assert parsed.directory == "one/two"
    assert git.parse_git_args(["--version"]).subcommand == ""


def test_commit_signing_is_neutralized_without_treating_option_values_as_flags():
    assert git.neutralize_commit_signing(["commit", "-S", "-m", "-S is text"]) == [
        "commit", "--no-gpg-sign", "-m", "-S is text"
    ]
    assert git.neutralize_commit_signing(["status", "-S"]) == ["status", "-S"]


def test_pr_creation_routes_and_urls_are_parsed():
    assert git.is_pr_create(["pr", "create", "--title", "x"])
    assert git.is_pr_create(["api", "repos/acme/app/pulls", "-f", "title=x"])
    assert not git.is_pr_create(["api", "repos/acme/app/pulls/1", "-X", "PATCH"])
    output = "body https://github.com/a/b/pull/1\nhttps://github.com/a/b/pull/2\n"
    assert git.find_pr_url(output).endswith("/1")
    assert git.find_last_pr_url(output).endswith("/2")
    assert git.parse_pr_url(json.dumps({"number": 3, "html_url": "https://github.com/a/b/pull/3"})).endswith("/3")
    assert not git.validate_pr_url("https://example.com/a/b/pull/3")


def test_gh_reports_only_successful_real_creation(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: completed(stdout="https://github.com/a/b/pull/4\n"))
    result = git.run_gh(["pr", "create"], report=calls.append)
    assert result.returncode == 0
    assert calls[0]["url"].endswith("/4")
    assert capsys.readouterr().out.endswith("/4\n")

    calls.clear()
    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: completed(1, "https://github.com/a/b/pull/5\n"))
    git.run_gh(["pr", "create"], report=calls.append)
    assert calls == []

    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: completed(stdout="https://github.com/a/b/pull/6\n"))
    git.run_gh(["pr", "create", "--dry-run"], report=calls.append)
    assert calls == []


def test_signed_push_fallback_is_specific_and_retries_once(monkeypatch):
    runs = iter([
        completed(1, stderr="remote: Commits must have verified signatures."),
        completed(0),
    ])
    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: next(runs))
    signed = []
    result = git.push_with_signing_fallback(
        ["-C", "repo", "push"], env={"GIT_CONFIG_COUNT": "1"},
        sign=lambda directory, env: signed.append((directory, env)),
    )
    assert result.returncode == 0
    assert signed[0][0] == "repo"
    assert "GIT_CONFIG_COUNT" not in signed[0][1]
    assert signed[0][1][git.SIGNING_GUARD] == "1"


def test_non_signature_failure_and_recursion_guard_do_not_sign(monkeypatch):
    calls = []
    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: completed(1, stderr="authentication failed"))
    assert git.push_with_signing_fallback(["push"], sign=lambda *a: calls.append(a)).returncode == 1
    assert calls == []

    git.push_with_signing_fallback(["push"], env={git.SIGNING_GUARD: "1"}, sign=lambda *a: calls.append(a))
    assert calls == []


def test_sign_request_preserves_chain_and_omits_app_author():
    commits = [
        git.Commit("a", "tree", ("base",), "first", "A", "a@example.com"),
        git.Commit("b", "tree2", ("a",), "second", git.GITHUB_BOT_NAME, git.GITHUB_BOT_EMAIL),
    ]
    request = git.sign_request(commits, "acme", "app", base_ref="origin/main", env={"GIT_CONFIG_COUNT": "1", "PATH": "x"})
    assert request["commits"][0]["original_author"]["email"] == "a@example.com"
    assert "original_author" not in request["commits"][1]
    assert request["base_ref"] == "origin/main"
    assert request["env"] == {"PATH": "x"}
    assert git.first_unsigned_commit([git.Commit("a", "t", (), "m", git.GITHUB_BOT_NAME, git.GITHUB_BOT_EMAIL)]) == -1


def test_origin_parser_accepts_https_and_ssh():
    assert git.origin_owner_repo(remote="https://github.com/acme/app.git") == ("acme", "app")
    assert git.origin_owner_repo(remote="git@github.com:acme/app.git") == ("acme", "app")


def test_agent_environment_scrubs_control_plane_secrets():
    env = main.agent_environment({
        "PATH": "/usr/bin", "HATCHERY_DAEMON_TOKEN": "secret", "VERCEL_QUEUE_TOKEN": "queue",
        "GH_TOKEN": "github", "SAFE": "yes",
    })
    assert env["SAFE"] == "yes"
    assert env["PATH"].startswith("/opt/hatchery/bin:")
    assert "HATCHERY_DAEMON_TOKEN" not in env
    assert "VERCEL_QUEUE_TOKEN" not in env
    assert "GH_TOKEN" not in env
