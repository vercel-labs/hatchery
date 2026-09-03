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
    request = git.sign_request(
        commits,
        "acme",
        "app",
        base_oid="a" * 40,
        branch="hatchery/sign-1",
        env={"GIT_CONFIG_COUNT": "1", "PATH": "x"},
    )
    assert request["commits"][0]["original_author"]["email"] == "a@example.com"
    assert "original_author" not in request["commits"][1]
    assert request["base_oid"] == "a" * 40
    assert request["branch"] == "hatchery/sign-1"
    assert request["env"] == {"PATH": "x"}
    assert git.first_unsigned_commit([git.Commit("a", "t", (), "m", git.GITHUB_BOT_NAME, git.GITHUB_BOT_EMAIL)]) == -1


def test_commit_chain_uses_local_main_when_remote_tracking_refs_are_absent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "switch", "-qc", "feature"], check=True)
    (repo / "README.md").write_text("base\nchange\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "change"], check=True)

    found_base, commits = git._commit_chain(str(repo), dict(__import__("os").environ))

    assert found_base == base
    assert [commit.message.strip() for commit in commits] == ["change"]


def test_commit_changes_are_read_locally_without_uploading_unsigned_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "a@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"],
        check=True,
    )
    (repo / "old.txt").write_text("old\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "old.txt").unlink()
    (repo / "new.txt").write_bytes(b"new\x00content")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    assert git._commit_changes(str(repo), dict(__import__("os").environ), base, head) == {
        "additions": [
            {
                "path": "new.txt",
                "contents": __import__("base64").b64encode(b"new\x00content").decode(),
            }
        ],
        "deletions": [{"path": "old.txt"}],
    }


def test_sign_chain_sends_local_changes_without_unsigned_source_ref(monkeypatch):
    commit = git.Commit(
        "b" * 40,
        "tree",
        ("a" * 40,),
        "change",
        "A",
        "a@example.com",
    )
    monkeypatch.setattr(git, "_commit_chain", lambda *_: ("a" * 40, [commit]))
    monkeypatch.setattr(git, "origin_owner_repo", lambda *_: ("acme", "app"))
    monkeypatch.setattr(
        git,
        "_commit_changes",
        lambda *_: {"additions": [{"path": "new.txt", "contents": "bmV3"}], "deletions": []},
    )
    calls = []

    def run_git(_repo, _env, *args):
        calls.append(args)
        return ""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "data": {
                    "createCommitOnBranch": {
                        "commit": {"oid": "c" * 40, "signature": {"isValid": True}}
                    }
                }
            }).encode()

    requests = []

    def urlopen(request, timeout):
        requests.append((json.loads(request.data), timeout, request.full_url, request.headers))
        return Response()

    monkeypatch.setattr(git, "_git", run_git)
    monkeypatch.setattr(git.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(git.subprocess, "run", lambda *args, **kwargs: completed())

    git._sign_chain("repo", {})

    assert calls[0][:3] == (
        "push",
        "https://github.com/acme/app.git",
        next(arg for arg in calls[0] if arg.startswith("a" * 40 + ":refs/heads/hatchery/sign-")),
    )
    assert not any("source-" in arg for call in calls for arg in call)
    request, timeout, url, headers = requests[0]
    assert timeout == 60
    assert url == "https://api.github.com/graphql"
    assert headers["Authorization"] == "Bearer sandbox-network-policy-placeholder"
    assert request["query"] == git.CREATE_COMMIT_MUTATION
    assert request["variables"]["input"]["fileChanges"]["additions"][0]["path"] == "new.txt"
    assert calls[-2:] == [
        ("fetch", "https://github.com/acme/app.git", "c" * 40),
        ("reset", "--hard", "c" * 40),
    ]


def test_origin_parser_accepts_https_and_ssh():
    assert git.origin_owner_repo(remote="https://github.com/acme/app.git") == ("acme", "app")
    assert git.origin_owner_repo(remote="git@github.com:acme/app.git") == ("acme", "app")


def test_agent_environment_scrubs_control_plane_secrets():
    env = main.agent_environment({
        "PATH": "/usr/bin", "HATCHERY_DAEMON_TOKEN": "secret",
        "VERCEL_QUEUE_TOKEN": "queue",
        "GH_TOKEN": "github", "AI_GATEWAY_API_KEY": "gateway-key", "SAFE": "yes",
    })
    assert env["SAFE"] == "yes"
    assert env["AI_GATEWAY_API_KEY"] == "gateway-key"
    assert env["PATH"].startswith("/opt/hatchery/bin:")
    assert "HATCHERY_DAEMON_TOKEN" not in env
    assert "VERCEL_QUEUE_TOKEN" not in env
    assert env["GH_TOKEN"] == "sandbox-network-policy-placeholder"
    assert env["GH_TOKEN"] != "github"
