import base64
import contextlib
import dataclasses
import os
import re
import subprocess
import types

import httpx
import pytest

from hatchery.worker import dependencies, models, provider, sandbox, store, transfer
from hatchery.workspace import files


async def test_provision_creates_persistent_sandbox_and_checks_daemon(monkeypatch):
    calls = {}

    class Files:
        async def mkdir(self, path):
            calls["mkdir"] = path

        async def write_text(self, path, text, mode):
            calls["write"] = (path, text, mode)

    class Process:
        returncode = None

        async def refresh(self):
            pass

    class Box:
        fs = Files()
        region = "iad1"
        routes = [
            types.SimpleNamespace(port=8787, url="https://daemon.example"),
            types.SimpleNamespace(port=8788, url="https://ssh.example"),
        ]

        async def update(self, **options):
            calls["update"] = options
            return self

        async def update_network_policy(self, policy):
            calls["network_policy"] = policy

        async def run_process(self, command, args, **options):
            calls.setdefault("runs", []).append((command, args, options))

        async def create_process(self, command, args, env):
            calls["process"] = (command, args, env)
            return Process()

    async def get_or_create_sandbox(**options):
        calls["options"] = options
        return Box(), True

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ok": True,
                "version": sandbox.daemon_main.VERSION,
                "queue_connected": True,
            }

    class Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers):
            calls["health"] = (url, headers)
            return Response()

    async def oidc_token():
        return "oidc-token"

    async def github_credential(user_id, *, required=True):
        calls["github_user"] = (user_id, required)
        return "user-token"

    async def git_identity(user_id):
        assert user_id == "user_1"
        return "The Octocat", "42+octocat@users.noreply.github.com"

    monkeypatch.setattr(
        sandbox.vercel_sandbox, "get_or_create_sandbox", get_or_create_sandbox
    )
    monkeypatch.setattr(sandbox.vercel_oidc, "get_vercel_oidc_token", oidc_token)
    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sandbox, "_github_credential", github_credential)
    monkeypatch.setattr(sandbox, "_git_identity", git_identity)

    provisioned = await sandbox.provision(
        "wrk_1", models.WorkerSpec(), "secret", user_id="user_1"
    )

    assert provisioned.sandbox_name == "hatchery-wrk_1"
    assert calls["github_user"] == ("user_1", False)
    github_rule = calls["network_policy"].allow["api.github.com"][0]
    assert dict(github_rule.transform[0].headers) == {
        "Authorization": "Bearer user-token"
    }
    assert calls["options"]["persistent"] is True
    assert calls["options"]["resources"].vcpus == 2
    assert calls["options"]["resources"].memory == 4096
    assert calls["options"]["tags"] == {
        "hatchery-worker": "wrk_1",
        "hatchery-size": "small",
    }
    assert calls["options"]["execution_time_limit"] == sandbox.EXECUTION_TIME_LIMIT
    assert calls["update"] == {"execution_time_limit": sandbox.EXECUTION_TIME_LIMIT}
    bootstrap = calls["runs"][0][1][1]
    for package in ("vercel-queue", "vercel-connect", "asyncssh", "websockets"):
        assert package in bootstrap
    assert "from vercel import connect, queue; import asyncssh, websockets" in bootstrap
    startup = calls["process"][1][1]
    assert "FX_INSTALL_DIR=/opt/hatchery/bin bash -s -- v0.0.8" in startup
    assert 'test "$(/opt/hatchery/bin/fx --version)" = "0.0.8"' in startup
    assert calls["options"]["ports"] == [8787, 8788]
    assert calls["options"]["env"] == {
        "AI_GATEWAY_API_KEY": sandbox.AI_GATEWAY_PLACEHOLDER,
    }
    gateway_rule = calls["network_policy"].allow[sandbox.AI_GATEWAY_HOST][0]
    assert dict(gateway_rule.transform[0].headers) == {
        "Authorization": "Bearer oidc-token",
        "ai-gateway-auth-method": "oidc",
    }
    queue_rule = calls["network_policy"].allow["iad1.vercel-queue.com"][0]
    assert dict(queue_rule.transform[0].headers) == {
        "Authorization": "Bearer oidc-token"
    }
    assert queue_rule.match.headers[0].value.value == (
        f"Bearer {sandbox.QUEUE_TOKEN_PLACEHOLDER}"
    )
    assert calls["process"][2]["HATCHERY_DAEMON_TOKEN"] == "secret"
    assert calls["process"][2]["HATCHERY_WORKER_ID"] == "wrk_1"
    assert "HATCHERY_EVENT_DEPLOYMENT" not in calls["process"][2]
    assert calls["process"][2]["VERCEL_REGION"] == "iad1"
    assert calls["process"][2]["VERCEL_QUEUE_TOKEN"] == sandbox.QUEUE_TOKEN_PLACEHOLDER
    assert "VERCEL_OIDC_TOKEN" not in calls["process"][2]
    assert "VERCEL_DEPLOYMENT_ID" not in calls["process"][2]
    assert calls["process"][2]["FX_PERMISSION_MODE"] == "yolo"
    assert calls["process"][2]["AI_GATEWAY_API_KEY"] == sandbox.AI_GATEWAY_PLACEHOLDER
    assert calls["health"] == (
        "https://daemon.example/health",
        {"authorization": "Bearer secret"},
    )


@pytest.mark.parametrize(
    "installed,downloaded,passes",
    [
        (None, "0.0.8", True),
        ("0.0.7", "0.0.8", True),
        ("0.0.9", "0.0.8", True),
        ("0.0.8", None, True),
        ("0.0.7", None, False),
        (None, "0.0.9", False),
    ],
)
async def test_daemon_startup_enforces_fx_pin(
    monkeypatch, tmp_path, installed, downloaded, passes
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fx = bin_dir / "fx"
    if installed is not None:
        fx.write_text(f"#!/bin/sh\nprintf '%s\\n' '{installed}'\n")
        fx.chmod(0o755)
    fetched = tmp_path / "fetched"
    installer = tmp_path / "setup.sh"
    installer.write_text(
        "set -eu\n"
        'test "$1" = v0.0.8\n'
        'mkdir -p "$FX_INSTALL_DIR"\n'
        "cat > \"$FX_INSTALL_DIR/fx\" <<'EOF'\n"
        f"#!/bin/sh\nprintf '%s\\n' '{downloaded}'\n"
        "EOF\n"
        'chmod +x "$FX_INSTALL_DIR/fx"\n'
    )
    curl = bin_dir / "curl"
    curl.write_text(
        f"#!/bin/sh\ntouch '{fetched}'\n"
        + (f"cat '{installer}'\n" if downloaded is not None else "exit 22\n")
    )
    curl.chmod(0o755)
    monkeypatch.setattr(sandbox, "SHIM_PATH", str(bin_dir))
    monkeypatch.setattr(sandbox.daemon_main, "FX_BINARY", str(fx))
    scripts = []

    class Box:
        region = "iad1"

        async def create_process(self, command, args, env):
            scripts.append(args[1])

    await sandbox._start_daemon(Box(), "wrk", models.WorkerSpec(), "secret")
    # Exercise the actual installation/version-check prefix without starting
    # a daemon or killing processes. The downloader is an offline fixture.
    script = scripts[0].split("pkill -f", 1)[0] + "printf admitted"
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) is passes, result.stderr
    assert ("admitted" in result.stdout) is passes
    assert fetched.exists() is (installed != "0.0.8")
    if passes:
        assert (
            subprocess.check_output([str(fx), "--version"], text=True).strip()
            == "0.0.8"
        )


def test_worker_spec_sizes_resolve_to_resources():
    assert models.WorkerSpec().resolved_resources() == (2, 4096)
    assert models.WorkerSpec(size="big").resolved_resources() == (4, 8192)


async def test_is_live_passively_checks_running_status(monkeypatch):
    calls = []

    async def get_sandbox(*, name):
        calls.append(name)
        return types.SimpleNamespace(
            status=sandbox.vercel_sandbox.SandboxStatus.RUNNING
        )

    async def resume_sandbox(**_options):
        raise AssertionError("liveness check must not resume the sandbox")

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_sandbox", get_sandbox)
    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)

    assert await sandbox.is_live("hatchery-wrk_1") is True
    assert calls == ["hatchery-wrk_1"]


async def test_is_live_is_false_for_stopped_sandbox_and_lookup_error(monkeypatch):
    async def stopped(*, name):
        assert name == "hatchery-wrk_1"
        return types.SimpleNamespace(
            status=sandbox.vercel_sandbox.SandboxStatus.STOPPED
        )

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_sandbox", stopped)
    assert await sandbox.is_live("hatchery-wrk_1") is False

    async def failed(*, name):
        raise RuntimeError(f"cannot find {name}")

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_sandbox", failed)
    assert await sandbox.is_live("hatchery-wrk_1") is False


def test_workspace_matches_vercel_git_source_layout():
    assert sandbox._workspace(models.WorkerSpec(repos=["acme/app"])) == "/vercel/app"
    assert sandbox._workspace(models.WorkerSpec()) == "/vercel"


async def test_setup_script_gets_github_placeholder(monkeypatch):
    calls = []

    class Files:
        async def mkdir(self, path):
            pass

        async def write_text(self, path, text, mode):
            pass

    class Box:
        fs = Files()
        region = "iad1"

        async def run_process(self, command, args, **options):
            calls.append((command, args, options))

        async def create_process(self, command, args, env):
            return object()

    async def identity(_user_id):
        return None

    async def start(*args, **kwargs):
        return object()

    monkeypatch.setattr(sandbox, "_git_identity", identity)
    monkeypatch.setattr(sandbox, "_start_daemon", start)

    await sandbox._bootstrap(
        Box(),
        "wrk_1",
        models.WorkerSpec(setup_script="gh repo clone acme/app app"),
        "secret",
        identity=None,
    )

    setup = next(
        call for call in calls if call[1] == ["-lc", "gh repo clone acme/app app"]
    )
    assert setup[2]["env"] == {"GH_TOKEN": sandbox.GITHUB_TOKEN_PLACEHOLDER}


async def test_github_credential_uses_connected_user(monkeypatch):
    seen = []

    async def token(user_id):
        seen.append(user_id)
        return "user-token"

    monkeypatch.setattr(sandbox.connections, "github_token", token)

    assert await sandbox._github_credential("user_1") == "user-token"
    assert seen == ["user_1"]


async def test_empty_sandbox_allows_missing_github_connection(monkeypatch):
    async def token(_user_id):
        raise sandbox.connections.ConnectionRequired(
            httpx.Response(
                401, request=httpx.Request("GET", "https://connect.vercel.com")
            ),
            "connect",
        )

    monkeypatch.setattr(sandbox.connections, "github_token", token)

    assert await sandbox._github_credential("user_1", required=False) is None


async def test_git_identity_uses_connected_github_profile(monkeypatch):
    async def identity(_user_id):
        return {"id": "42", "login": "octocat", "name": "The Octocat"}

    monkeypatch.setattr(sandbox.connections, "github_identity", identity)

    assert await sandbox._git_identity("user_1") == (
        "The Octocat",
        "42+octocat@users.noreply.github.com",
    )


async def test_canonicalize_repos_follows_github_transfer(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"full_name": "new-owner/app"}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["authorization"] == "Bearer user-token"
            assert kwargs["follow_redirects"] is True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, path):
            assert path == "/repos/old-owner/app"
            return Response()

    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)
    spec = models.WorkerSpec(repos=["old-owner/app"])

    await sandbox._canonicalize_repos(spec, "user-token")

    assert spec.repos == ["new-owner/app"]


async def test_configure_repo_remotes_uses_canonical_names():
    calls = []

    class Box:
        async def run_process(self, command, args, **options):
            calls.append((command, args, options))

    await sandbox._configure_repo_remotes(
        Box(), models.WorkerSpec(repos=["new-owner/app"])
    )

    assert calls == [
        (
            "git",
            [
                "-C",
                "/vercel/app",
                "remote",
                "set-url",
                "origin",
                "https://github.com/new-owner/app.git",
            ],
            {"check": True, "capture_output": True},
        )
    ]


async def test_wait_for_daemon_retries_route_warmup(monkeypatch):
    calls = 0

    class Response:
        def raise_for_status(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.HTTPStatusError(
                    "bad gateway",
                    request=httpx.Request("GET", "https://daemon.example/health"),
                    response=httpx.Response(502),
                )

        def json(self):
            return {
                "ok": True,
                "version": sandbox.daemon_main.VERSION,
                "queue_connected": True,
            }

    class Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers):
            return Response()

    async def sleep(delay):
        pass

    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sandbox.asyncio, "sleep", sleep)

    health = await sandbox._wait_for_daemon("https://daemon.example", "secret")

    assert health == {
        "ok": True,
        "version": sandbox.daemon_main.VERSION,
        "queue_connected": True,
    }
    assert calls == 2


async def test_wait_for_daemon_reports_process_failure(monkeypatch):
    class Process:
        returncode = 1

        async def refresh(self):
            pass

        async def communicate(self):
            return "", "ImportError: cannot import name 'connect' from 'vercel'"

    async def daemon_health(url, token):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sandbox, "_daemon_health", daemon_health)

    try:
        await sandbox._wait_for_daemon("https://daemon.example", "secret", Process())
    except RuntimeError as error:
        assert str(error) == (
            "sandbox daemon exited with 1: "
            "ImportError: cannot import name 'connect' from 'vercel'"
        )
    else:
        raise AssertionError("exited daemon should fail with its stderr")


async def test_existing_sandbox_repairs_dead_daemon(monkeypatch):
    calls = {}

    class Files:
        async def mkdir(self, path):
            calls.setdefault("mkdir", []).append(path)

        async def write_text(self, path, text, mode):
            calls["write"] = (path, mode)

    class Process:
        returncode = None

        async def refresh(self):
            pass

    class Box:
        fs = Files()
        region = "iad1"
        routes = [types.SimpleNamespace(port=8787, url="https://daemon.example")]

        async def create_process(self, command, args, env):
            calls["process"] = (command, args, env)
            return Process()

    health = iter(
        [
            httpx.ConnectError("down"),
            {
                "ok": True,
                "version": sandbox.daemon_main.VERSION,
                "queue_connected": True,
            },
        ]
    )

    async def daemon_health(url, token):
        result = next(health)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sandbox, "_daemon_health", daemon_health)
    monkeypatch.setattr(
        sandbox,
        "_wait_for_daemon",
        lambda *args, **kwargs: daemon_health(args[0], args[1]),
    )

    await sandbox.repair_daemon(
        Box(),
        "wrk_1",
        models.WorkerSpec(),
        "secret",
        [models.Route(port=8787, url="https://daemon.example")],
    )

    assert calls["write"] == (sandbox.DAEMON_PATH, 0o755)
    assert calls["process"][0] == "/bin/sh"
    assert "pkill" in calls["process"][1][1]
    assert f"exec python3 {sandbox.DAEMON_PATH}" in calls["process"][1][1]
    assert f">>{sandbox.DAEMON_LOG_PATH} 2>&1" in calls["process"][1][1]


async def test_healthy_daemon_is_restarted_when_event_deployment_changes(monkeypatch):
    calls = {}

    class Files:
        async def mkdir(self, path):
            pass

        async def write_text(self, path, text, mode):
            calls["write"] = (path, mode)

    class Box:
        fs = Files()
        region = "iad1"

        async def create_process(self, command, args, env):
            calls["env"] = env
            return types.SimpleNamespace(returncode=None)

    async def daemon_health(url, token):
        return {
            "ok": True,
            "version": sandbox.daemon_main.VERSION,
            "queue_connected": True,
            "event_deployment": "dpl_old",
        }

    async def wait_for_daemon(url, token, process):
        return {
            "ok": True,
            "version": sandbox.daemon_main.VERSION,
            "queue_connected": True,
            "event_deployment": "dpl_new",
        }

    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_new")
    monkeypatch.setattr(sandbox, "_daemon_health", daemon_health)
    monkeypatch.setattr(sandbox, "_wait_for_daemon", wait_for_daemon)

    await sandbox.repair_daemon(
        Box(),
        "wrk_1",
        models.WorkerSpec(),
        "secret",
        [models.Route(port=8787, url="https://daemon.example")],
    )

    assert calls["write"] == (sandbox.DAEMON_PATH, 0o755)
    assert calls["env"]["HATCHERY_EVENT_DEPLOYMENT"] == "dpl_new"


async def test_prepare_for_command_resumes_and_repairs_daemon(monkeypatch):
    calls = []

    class Box:
        region = "iad1"
        routes = [types.SimpleNamespace(port=8787, url="https://daemon.example")]

        async def update(self, **options):
            calls.append(("update", options))
            return self

        async def update_network_policy(self, policy):
            calls.append(("policy", policy))

    async def resume_sandbox(name):
        calls.append(("resume", name))
        return Box()

    async def credentials():
        return None

    async def network_policy(credential, region):
        assert (credential, region) == (None, "iad1")
        return "queue-policy"

    async def configure(box, identity):
        calls.append(("git", box.region, identity))

    async def repair(box, worker_id, spec, token, routes):
        calls.append(("repair", worker_id, token, routes))

    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(sandbox.git, "git_credentials", credentials)

    async def identity(user_id):
        assert user_id == "user_actor"
        return None

    async def github_credential(user_id, *, required=True):
        assert user_id == "user_actor"
        return None

    monkeypatch.setattr(sandbox.git, "configure", configure)
    monkeypatch.setattr(sandbox, "_git_identity", identity)
    monkeypatch.setattr(sandbox, "_github_credential", github_credential)
    monkeypatch.setattr(sandbox, "_network_policy", network_policy)
    monkeypatch.setattr(sandbox, "repair_daemon", repair)
    record = models.Worker(
        id="wrk_1",
        chat_id="chat_1",
        sandbox_name="hatchery-wrk_1",
        command_topic="topic",
        title="worker",
        status="running",
        spec=models.WorkerSpec(),
        daemon_token="secret",
        created_at="now",
        updated_at="now",
    )

    await sandbox.prepare_for_command(record, actor_user_id="user_actor")

    assert calls == [
        ("resume", "hatchery-wrk_1"),
        ("update", {"execution_time_limit": sandbox.EXECUTION_TIME_LIMIT}),
        ("policy", "queue-policy"),
        ("git", "iad1", None),
        (
            "repair",
            "wrk_1",
            "secret",
            [models.Route(port=8787, url="https://daemon.example")],
        ),
    ]


async def test_prepare_for_tty_only_resumes_sandbox(monkeypatch):
    calls = []

    class Box:
        region = "iad1"

        async def update(self, **options):
            calls.append(("update", options))

    async def resume_sandbox(name):
        calls.append(("resume", name))
        return Box()

    async def repair(*args):
        raise AssertionError("TTY attachment must not replace its daemon")

    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(sandbox, "repair_daemon", repair)
    record = models.Worker(
        id="wrk_1",
        chat_id="chat_1",
        sandbox_name="hatchery-wrk_1",
        command_topic="topic",
        title="worker",
        status="running",
        spec=models.WorkerSpec(),
        daemon_token="secret",
        created_at="now",
        updated_at="now",
    )

    await sandbox.prepare_for_tty(record)

    assert calls == [
        ("resume", "hatchery-wrk_1"),
        ("update", {"execution_time_limit": sandbox.EXECUTION_TIME_LIMIT}),
    ]


async def test_probe_route_rejects_undeclared_port():
    record = models.Worker(
        id="wrk_1",
        chat_id="chat_1",
        sandbox_name="hatchery-wrk_1",
        command_topic="topic",
        title="worker",
        status="running",
        spec=models.WorkerSpec(ports=[3000]),
        routes=[models.Route(port=3000, url="https://app.example")],
        daemon_token="secret",
        created_at="now",
        updated_at="now",
    )

    try:
        await sandbox.probe_route(record, 8787)
    except ValueError as error:
        assert "not declared" in str(error)
    else:
        raise AssertionError("control-plane route must not be probeable")


async def test_snapshot_create_and_restore(monkeypatch):
    calls = []

    class Created:
        id = "snap_1"

    class Box:
        region = "iad1"
        routes = [types.SimpleNamespace(port=8787, url="https://daemon.example")]

        async def snapshot(self):
            calls.append("snapshot")
            return Created()

        async def stop(self):
            calls.append("stop")

        async def update(self, **options):
            calls.append(("update", options))

        async def update_network_policy(self, policy):
            calls.append("policy")

    box = Box()

    async def get_sandbox(name):
        return box

    async def resume_sandbox(name):
        calls.append("resume")
        return box

    async def configure(found, identity=None):
        calls.append("git")

    async def credentials():
        return None

    async def github_credential(_user_id, *, required=True):
        return None

    async def network_policy(credential, region):
        assert region == "iad1"
        return "policy"

    async def repair(*args, **kwargs):
        calls.append("repair")

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_sandbox", get_sandbox)
    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(sandbox.git, "configure", configure)
    monkeypatch.setattr(sandbox.git, "git_credentials", credentials)
    monkeypatch.setattr(sandbox, "_github_credential", github_credential)
    monkeypatch.setattr(sandbox, "_network_policy", network_policy)
    monkeypatch.setattr(sandbox, "repair_daemon", repair)
    record = models.Worker(
        id="wrk_1",
        chat_id="chat_1",
        sandbox_name="hatchery-wrk_1",
        command_topic="topic",
        title="worker",
        status="running",
        spec=models.WorkerSpec(),
        routes=[models.Route(port=8787, url="https://daemon.example")],
        daemon_token="secret",
        created_at="now",
        updated_at="now",
    )

    assert await sandbox.snapshot(record) == "snap_1"
    assert await sandbox.snapshot(record, "snap_1") == "snap_1"
    assert calls == [
        "snapshot",
        "stop",
        ("update", {"current_snapshot_id": "snap_1"}),
        "resume",
        "policy",
        "git",
        "repair",
    ]


def test_daemon_env_bridges_vercel_dev_queue_through_public_origin(monkeypatch):
    monkeypatch.setenv("VERCEL_QUEUE_TOKEN", "vc-dev-token")
    monkeypatch.setenv("VERCEL_QUEUE_BASE_URL", "http://127.0.0.1:3000/_svc/_queues")
    monkeypatch.setenv("VERCEL_REGION", "dev1")
    monkeypatch.setenv("HATCHERY_PUBLIC_URL", "https://hatchery.vgrok.example/")

    env = sandbox._daemon_env("wrk_1", models.WorkerSpec(), "secret")

    assert env["VERCEL_QUEUE_TOKEN"] == "vc-dev-token"
    assert env["VERCEL_QUEUE_BASE_URL"] == (
        "https://hatchery.vgrok.example/_svc/_queues"
    )
    assert env["VERCEL_REGION"] == "dev1"
    assert "VERCEL_DEPLOYMENT_ID" not in env


def test_daemon_env_uses_placeholder_without_exposing_cloud_identity(monkeypatch):
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc")
    monkeypatch.setenv("VERCEL_REGION", "iad1")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_1")
    monkeypatch.setenv("VERCEL_QUEUE_BASE_URL", "https://queues.example")
    monkeypatch.delenv("VERCEL_QUEUE_TOKEN", raising=False)

    env = sandbox._daemon_env("wrk_1", models.WorkerSpec(), "secret", region="sfo1")

    assert "VERCEL_OIDC_TOKEN" not in env
    assert "VERCEL_DEPLOYMENT_ID" not in env
    assert env["HATCHERY_EVENT_DEPLOYMENT"] == "dpl_1"
    assert env["VERCEL_QUEUE_TOKEN"] == sandbox.QUEUE_TOKEN_PLACEHOLDER
    assert env["VERCEL_REGION"] == "iad1"
    assert env["VERCEL_QUEUE_BASE_URL"] == "https://queues.example"


def test_daemon_env_uses_sandbox_region_when_runtime_region_is_missing(monkeypatch):
    monkeypatch.delenv("VERCEL_REGION", raising=False)

    env = sandbox._daemon_env("wrk_1", models.WorkerSpec(), "secret", region="iad1")

    assert env["VERCEL_REGION"] == "iad1"


def test_daemon_env_requires_public_origin_for_vercel_dev(monkeypatch):
    monkeypatch.setenv("VERCEL_QUEUE_TOKEN", "vc-dev-token")
    monkeypatch.delenv("HATCHERY_PUBLIC_URL", raising=False)

    try:
        sandbox._daemon_env("wrk_1", models.WorkerSpec(), "secret")
    except RuntimeError as error:
        assert "HATCHERY_PUBLIC_URL" in str(error)
    else:
        raise AssertionError("missing public origin should fail")


# The agentmesh provider contract (ported from agentmesh tests/unit/test_vercel_sandbox.py).
# The Vercel SDK is an owned fake of the adapter's small surface; this certifies the
# adapter's use of that surface, not live microVM behavior.

AGENT_THEN_RUNTIME = (
    r"/workspace/self/scripts:/workspace/\.hatchery/runtime/[0-9a-f]{64}/scripts:"
)


def seed(tree: dict[str, bytes] | None = None) -> provider.Seed:
    async def build() -> files.Tree:
        return {path: files.File(content) for path, content in (tree or {}).items()}

    return build


class ApiError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class CredentialsError(Exception):
    pass


@dataclasses.dataclass
class Completed:
    returncode: int


@dataclasses.dataclass(frozen=True)
class FakeEntry:
    path: str
    kind: str


class FakeReader:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def read(self, size: int = -1) -> bytes:
        return self.content if size < 0 else self.content[:size]


@dataclasses.dataclass
class FakeFS:
    files: dict[str, bytes] = dataclasses.field(default_factory=dict)
    directories: set[str] = dataclasses.field(default_factory=set)

    async def exists(self, path):
        return path in self.files

    async def write_bytes(self, path, data, **_):
        self.files[path] = data

    async def write_text(self, path, text, **_):
        self.files[path] = text.encode()

    async def remove(self, path, **_):
        self.files.pop(path, None)

    async def mkdir(self, path, **_):
        self.directories.add(path)

    async def listdir(self, path):
        prefix = path.rstrip("/") + "/"
        children: dict[str, str] = {}
        for candidate in (*self.directories, *self.files):
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            if not remainder:
                continue
            child, separator, _ = remainder.partition("/")
            children[child] = (
                "directory" if separator or candidate in self.directories else "file"
            )
        return [FakeEntry(child, kind) for child, kind in sorted(children.items())]

    def open(self, path, mode):
        assert mode == "rb"
        return FakeReader(self.files[path])


class FakeRemote:
    """A VM: `workspace` is /workspace as a tree; commands are interpreted, not executed."""

    def __init__(self, name, tags, persistent=True):
        self.name, self.tags, self.persistent = name, tags, persistent
        self.created_at = 42
        self.fs = FakeFS()
        self.workspace: dict[str, files.File] = {}
        self.runs: list[dict] = []
        self.stopped = self.destroyed = False
        self.exit_code = 0
        self.raise_on_run: BaseException | None = None
        self.raise_on_stop: BaseException | None = None
        self.dependency_exit_code = 0
        self.dependency_error = ""
        self.network_policies: list = []

    async def run_process(self, command, args, **options):
        self.runs.append({"command": command, "args": args, **options})
        if self.raise_on_run is not None:
            raise self.raise_on_run
        script = " ".join(args)
        if command == "sh" and "tar -xf" in script:
            archive = args[-1].split("tar -xf ")[1].split(" ")[0]
            if "for p in " in script:  # a replace swaps whole roots
                roots = script.split("for p in ")[1].split(";")[0].split()
                self.workspace = {
                    path: file
                    for path, file in self.workspace.items()
                    if not any(path == r or path.startswith(f"{r}/") for r in roots)
                }
            self.workspace.update(transfer.unpack(self.fs.files.pop(archive), files.SANDBOX_ROOTS))
        elif command == "sh" and "--exclude=.git" in script and "base64 -w0" in script:
            roots = args[3:]
            selected = {
                p: f
                for p, f in self.workspace.items()
                if any(p == r or p.startswith(f"{r}/") for r in roots)
            }
            options["stdout"].write(base64.b64encode(transfer.pack(selected)).decode())
        elif command == "sh" and "rm -rf" in script:
            self.workspace.clear()
        elif command == "timeout" and args[-1] == dependencies.SYNC_SCRIPT:
            if self.dependency_error:
                options["stderr"].write(self.dependency_error)
            if self.dependency_exit_code == 0:
                options["stdout"].write(f"{dependencies.VENV_ROOT}/{'a' * 64}\n")
            return Completed(self.dependency_exit_code)
        elif command == "touch":
            self.fs.files[args[0]] = b""
        elif command == "timeout":
            options["stdout"].write("ran: " + args[-1])
            options["stderr"].write("warn")
            return Completed(self.exit_code)
        return Completed(0)

    async def stop(self):
        if self.raise_on_stop is not None:
            raise self.raise_on_stop
        self.stopped = True

    async def update_network_policy(self, policy):
        self.network_policies.append(policy)

    async def destroy(self):
        self.destroyed = True


class FakeSDK:
    SandboxApiError = ApiError
    SandboxCredentialsError = CredentialsError

    class SandboxResources:
        def __init__(self, **kw):
            self.kw = kw

    class NetworkPolicy:
        def __init__(self, mode):
            self.mode = mode

        @classmethod
        def allow_all(cls):
            return cls("allow-all")

        @classmethod
        def deny_all(cls):
            return cls("deny-all")

    class SnapshotRetention:
        def __init__(self, **kw):
            self.kw = kw

    def __init__(self):
        self.boxes: dict[str, FakeRemote] = {}
        self.creations: list[dict] = []
        self.conflict_once = False
        self.timeouts: list = []

    async def get_sandbox(self, *, name):
        if name not in self.boxes:
            raise ApiError(404)
        return self.boxes[name]

    async def get_or_create_sandbox(self, *, name, **options):
        self.creations.append(options)
        if self.conflict_once:
            self.conflict_once = False
            self.boxes[name] = FakeRemote(name, options["tags"])
            raise ApiError(409)
        if name not in self.boxes:
            self.boxes[name] = FakeRemote(name, options["tags"], options["persistent"])
        return self.boxes[name], True


@pytest.fixture
def sdk(monkeypatch):
    fake = FakeSDK()

    @contextlib.asynccontextmanager
    async def session(*, httpx_client_factory):
        client = httpx_client_factory()
        fake.timeouts.append(client.timeout)
        try:
            yield
        finally:
            await client.aclose()

    monkeypatch.setattr(sandbox, "vercel_sandbox", fake)
    monkeypatch.setattr(sandbox, "vercel_api", types.SimpleNamespace(session=session))
    return fake


async def test_fresh_sandbox_is_seeded_and_a_reacquire_keeps_edits(sdk):
    first = await sandbox.VercelSandboxProvider().acquire(
        "serve-1", seed=seed({"self/AGENTS.md": b"seed"}), purpose="serve"
    )
    remote = sdk.boxes["serve-1"]
    assert first.fresh and remote.workspace == {"self/AGENTS.md": files.File(b"seed")}
    assert sandbox.READY_MARKER in remote.fs.files
    assert {"/workspace/scratchpad", "/workspace/repos", "/workspace/.hatchery"} <= (
        remote.fs.directories
    )
    assert sdk.creations[0]["tags"] == {sandbox.OWNER_TAG: "hatchery"}
    assert sdk.creations[0]["persistent"]
    initialize = next(run for run in remote.runs if "mkdir -p" in " ".join(run["args"]))
    assert "/workspace/scratchpad" in " ".join(initialize["args"])
    protect = next(run for run in remote.runs if "chmod -R a-w" in " ".join(run["args"]))
    assert protect["sudo"] is True

    await first.sandbox.upload(
        {
            "self/notes.md": files.File(b"edited", executable=True),
            "scratchpad/plan.md": files.File(b"disposable"),
        }
    )
    again = await sandbox.VercelSandboxProvider().acquire(
        "serve-1", seed=seed({"self/AGENTS.md": b"newer"}), purpose="serve"
    )
    assert not again.fresh
    assert await again.sandbox.download(["self"]) == {
        "self/AGENTS.md": files.File(b"seed"),
        "self/notes.md": files.File(b"edited", executable=True),
    }
    assert await again.sandbox.download(["scratchpad"]) == {
        "scratchpad/plan.md": files.File(b"disposable")
    }
    assert await again.sandbox.download(["self/missing.md"]) == {}
    download = remote.runs[-1]
    assert "--ignore-failed-read" in download["args"][1]
    assert "mkdir -p" not in download["args"][1]


async def test_exec_runs_bash_under_timeout_in_self_and_reports_kills(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "serve-2", seed=seed(), purpose="serve"
    )
    result = await acquired.sandbox.exec("echo hi", timeout=30)
    run = sdk.boxes["serve-2"].runs[-1]
    assert run["command"] == "timeout"
    assert run["args"] == ["-k", "5", "30s", "bash", "--noprofile", "--norc", "-c", "echo hi"]
    assert run["cwd"] == "/workspace/self" and run["kill_after"] == 45
    assert re.match(
        rf"{AGENT_THEN_RUNTIME}{dependencies.VENV_ROOT}/a{{64}}/bin:", run["env"]["PATH"]
    )
    assert run["env"]["HOME"] == "/workspace/.hatchery/home"
    assert result == provider.ExecResult(0, "ran: echo hi", "warn")

    sdk.boxes["serve-2"].exit_code = 124
    killed = await acquired.sandbox.exec("sleep 999", timeout=7)
    assert killed.timed_out and "killed after 7s" in killed.stderr


async def test_runtime_helpers_are_installed_root_owned_and_follow_agent_scripts(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "helpers", seed=seed(), purpose="serve"
    )

    await acquired.sandbox.exec("search TODO", timeout=5)

    remote = sdk.boxes["helpers"]
    installs = {run["args"][-1]: run for run in remote.runs if run["command"] == "install"}
    helpers_dir = next(
        path.removesuffix("/scripts/search") for path in installs if path.endswith("/scripts/search")
    )
    assert re.fullmatch(r"/workspace/\.hatchery/runtime/[0-9a-f]{64}", helpers_dir)
    names = {"tree", "search", "edit", "fetch", "remember"}
    scripts = {f"{helpers_dir}/scripts/{name}" for name in names}
    sdk_files = {
        f"{helpers_dir}/hatchery/{path}"
        for path in ("__init__.py", "sdk/__init__.py", "sdk/__main__.py")
    }
    assert scripts | sdk_files == set(installs)
    # Scripts are executable, the SDK read-only; everything root-owned.
    assert all(installs[path]["args"][6] == "0555" for path in scripts)
    assert all(installs[path]["args"][6] == "0444" for path in sdk_files)
    assert all(run["sudo"] is True for run in installs.values())
    command = remote.runs[-1]
    assert command["env"]["PATH"].startswith(
        f"/workspace/self/scripts:{helpers_dir}/scripts:"
    )
    # `hatchery.sdk` resolves from the installed runtime.
    assert command["env"]["PYTHONPATH"] == f"/workspace/self:{helpers_dir}"
    with pytest.raises(ValueError, match="unsupported path"):
        await acquired.sandbox.install_runtime({"../escape": b"x"})


async def test_requirements_are_synchronized_before_commands_without_receiving_secrets(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "requirements",
        seed=seed({"self/requirements.txt": b"httpx==0.28.1\n"}),
        purpose="serve",
    )

    await acquired.sandbox.exec("python job.py", timeout=12, env={"ROUTE_SECRET": "private"})

    runs = sdk.boxes["requirements"].runs
    sync = next(run for run in runs if run["args"][-1] == dependencies.SYNC_SCRIPT)
    command = runs[-1]
    assert sync["command"] == "timeout"
    assert sync["args"][:5] == [
        "-k",
        "5",
        f"{dependencies.INSTALL_TIMEOUT_SECONDS}s",
        "sh",
        "-c",
    ]
    assert sync["env"] == {}
    assert command["env"]["ROUTE_SECRET"] == "private"
    assert re.match(
        rf"{AGENT_THEN_RUNTIME}{dependencies.VENV_ROOT}/a{{64}}/bin:", command["env"]["PATH"]
    )


async def test_failed_requirements_warn_but_do_not_block_the_repair_command(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "broken-requirements",
        seed=seed({"self/requirements.txt": b"not-a-real-package\n"}),
        purpose="serve",
    )
    remote = sdk.boxes["broken-requirements"]
    remote.dependency_exit_code = 1
    remote.dependency_error = "No matching distribution found"

    result = await acquired.sandbox.exec("rm requirements.txt", timeout=9)

    run = remote.runs[-1]
    assert run["args"][-1] == "rm requirements.txt"
    assert re.fullmatch(rf"{AGENT_THEN_RUNTIME}/usr/local/bin:/usr/bin:/bin", run["env"]["PATH"])
    assert result.exit_code == 0
    assert "workspace requirements installation failed" in result.stderr
    assert "No matching distribution found" in result.stderr


async def test_stdin_is_staged_without_passing_an_unsupported_sdk_argument(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "stdin", seed=seed(), purpose="serve"
    )

    await acquired.sandbox.exec("python handler.py", timeout=5, stdin=b'{"request":1}')

    run = sdk.boxes["stdin"].runs[-1]
    assert run["command"] == "sh"
    assert run["args"][1] == 'input="$1"; shift; exec "$@" < "$input"'
    assert "stdin" not in run
    assert not any(path.startswith("/tmp/hatchery-stdin-") for path in sdk.boxes["stdin"].fs.files)


async def test_serve_sandbox_has_persistent_data_and_reuses_requirements_setup(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "serve-reuse", seed=seed(), owner="agent-a", purpose="serve"
    )

    await acquired.sandbox.exec("first", timeout=5)
    await acquired.sandbox.exec("second", timeout=5)

    remote = sdk.boxes["serve-reuse"]
    assert "/workspace/data" in remote.fs.directories
    assert remote.network_policies[-1].mode == "allow-all"
    assert sum(run["args"][-1] == dependencies.SYNC_SCRIPT for run in remote.runs if run["args"]) == 1
    assert [run["args"][-1] for run in remote.runs if run["command"] == "timeout"][-2:] == [
        "first",
        "second",
    ]


async def test_command_stream_read_timeout_exceeds_the_remote_kill_deadline(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "stream-timeout", seed=seed(), purpose="serve"
    )
    await acquired.sandbox.exec("quiet job", timeout=30)

    command_timeout = sdk.timeouts[-1]
    assert command_timeout.read == 75  # 30s shell + 15s remote grace + 30s HTTP grace
    assert command_timeout.connect == 60


async def test_memory_replace_swaps_whole_roots(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "replace",
        seed=seed({"self/old.md": b"old", "wiki/keep.md": b"keep"}),
        purpose="serve",
    )
    remote = sdk.boxes["replace"]
    remote.workspace["scratchpad/notes.md"] = files.File(b"disposable")

    await acquired.sandbox.replace(
        ["self", "wiki"],
        {"self/new.md": files.File(b"new"), "wiki/keep.md": files.File(b"updated")},
    )

    assert remote.workspace == {
        "scratchpad/notes.md": files.File(b"disposable"),
        "self/new.md": files.File(b"new"),
        "wiki/keep.md": files.File(b"updated"),
    }
    with pytest.raises(ValueError, match="only self and wiki"):
        await acquired.sandbox.replace(["collective"], {})


async def test_a_lost_transport_stops_the_vm_so_nothing_outlives_it(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "lost", seed=seed(), purpose="serve"
    )
    remote = sdk.boxes["lost"]
    remote.raise_on_run = ConnectionError("gone")
    with pytest.raises(provider.SandboxCommandStopped, match="ConnectionError: gone") as raised:
        await acquired.sandbox.exec("long job", timeout=5)
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert remote.stopped
    with pytest.raises(RuntimeError, match="stopped"):
        await acquired.sandbox.exec("again", timeout=5)


async def test_a_lost_transport_is_uncertain_only_when_stopping_the_vm_fails(sdk):
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "uncertain", seed=seed(), purpose="serve"
    )
    remote = sdk.boxes["uncertain"]
    remote.raise_on_run = ConnectionError("stream gone")
    remote.raise_on_stop = ConnectionError("stop unconfirmed")

    with pytest.raises(provider.SandboxCommandUncertain, match="stop unconfirmed"):
        await acquired.sandbox.exec("long job", timeout=5)
    assert not remote.stopped


async def test_foreign_sandbox_names_are_refused_and_conflicts_recover_the_winner(sdk):
    sdk.boxes["taken"] = FakeRemote("taken", {sandbox.OWNER_TAG: "someone-else"})
    with pytest.raises(ValueError, match="foreign"):
        await sandbox.VercelSandboxProvider().acquire("taken", seed=seed(), purpose="serve")

    sdk.conflict_once = True
    acquired = await sandbox.VercelSandboxProvider().acquire(
        "racy", seed=seed(), purpose="serve"
    )
    assert acquired.fresh and acquired.sandbox.name == "racy"


async def test_release_keeps_or_destroys_only_the_same_incarnation(sdk):
    sandboxes = sandbox.VercelSandboxProvider()
    acquired = await sandboxes.acquire("serve-4", seed=seed(), purpose="serve")
    remote = sdk.boxes["serve-4"]
    await sandboxes.release(acquired.sandbox, keep=True)
    assert remote.stopped and not remote.destroyed

    replacement = FakeRemote("serve-4", {sandbox.OWNER_TAG: "hatchery"})
    replacement.created_at = 99
    sdk.boxes["serve-4"] = replacement
    await sandboxes.release(acquired.sandbox, keep=False)
    assert not replacement.destroyed, "an old handle never destroys a newer sandbox"

    sdk.boxes["serve-4"] = remote
    await sandboxes.release(acquired.sandbox, keep=False)
    assert remote.destroyed


async def test_missing_credentials_are_an_explicit_configuration_error(sdk):
    async def denied(**_):
        raise CredentialsError()

    sdk.get_sandbox = denied
    with pytest.raises(RuntimeError, match="VERCEL_TOKEN"):
        await sandbox.VercelSandboxProvider().acquire("x", seed=seed(), purpose="serve")


async def test_inspection_lists_hidden_files_and_bounds_previews_without_stopping(sdk):
    sandboxes = sandbox.VercelSandboxProvider()
    await sandboxes.acquire("inspect", seed=seed(), purpose="serve")
    remote = sdk.boxes["inspect"]
    remote.fs.files["/workspace/repos/app/untracked.log"] = b"abcdef"

    root = await sandboxes.list_directory("inspect")
    assert {entry.name for entry in root.entries} >= {".hatchery", "repos"}
    repository = await sandboxes.list_directory("inspect", "repos/app")
    assert repository.entries[0].path == "repos/app/untracked.log"
    preview = await sandboxes.read_file("inspect", repository.entries[0].path, limit=4)
    assert preview.content == b"abcd" and preview.truncated
    assert not remote.stopped
    with pytest.raises(FileNotFoundError):
        await sandboxes.list_directory("missing")


# Thread sandboxes are chat sandboxes: a worker record with the daemon.


@pytest.fixture
def daemon(sdk, monkeypatch):
    """Provisioning and daemon repair are covered above; record what the thread asks for."""
    calls = []

    async def provision(worker_id, spec, token, *, user_id=None):
        calls.append(("provision", worker_id, spec, token, user_id))
        name = f"hatchery-{worker_id}"
        sdk.boxes[name] = FakeRemote(name, {sandbox.WORKER_TAG: worker_id})
        return sandbox.Provisioned(name, [models.Route(port=8787, url="https://daemon.example")])

    async def prepare(record, *, actor_user_id=None):
        calls.append(("prepare", record.id, actor_user_id))

    monkeypatch.setattr(sandbox, "provision", provision)
    monkeypatch.setattr(sandbox, "prepare_for_command", prepare)
    return calls


async def test_thread_sandbox_is_a_chat_worker_seeded_once_and_resumed_by_name(sdk, daemon):
    sandboxes = sandbox.VercelSandboxProvider()
    name = provider.sandbox_name("hatchery:chat_1")
    chat = provider.ThreadChat("chat_1", "user_1", ("acme/app",))

    first = await sandboxes.acquire(
        name, seed=seed({"self/AGENTS.md": b"seed"}), owner="hatchery", chat=chat
    )

    worker_id = name.removeprefix("hatchery-")
    record = await store.get(worker_id)
    assert first.fresh
    assert [item.id for item in await store.list_all("chat_1")] == [worker_id]
    assert record.sandbox_name == name and record.status == "running"
    assert record.user_id == "user_1" and record.daemon_version == sandbox.daemon_main.VERSION
    assert record.spec.purpose == "thread" and record.spec.repos == ["acme/app"]
    assert record.routes == [models.Route(port=8787, url="https://daemon.example")]
    assert daemon == [("provision", worker_id, record.spec, record.daemon_token, "user_1")]
    remote = sdk.boxes[name]
    assert remote.workspace == {"self/AGENTS.md": files.File(b"seed")}

    await first.sandbox.exec("git status", timeout=5)
    command = remote.runs[-1]["env"]
    assert f":{sandbox.SHIM_PATH}:" in command["PATH"]
    assert command["GH_TOKEN"] == sandbox.GITHUB_TOKEN_PLACEHOLDER
    assert "HOME" not in command, "thread commands share the chat sandbox's HOME"

    await first.sandbox.upload({"self/notes.md": files.File(b"uncommitted")})
    await sandboxes.release(first.sandbox, keep=True)
    assert remote.stopped and not remote.destroyed
    assert (await store.get(worker_id)).status == "stopped"

    again = await sandboxes.acquire(
        name, seed=seed({"self/AGENTS.md": b"newer"}), chat=provider.ThreadChat("chat_1", "user_2")
    )
    assert not again.fresh
    assert daemon[-1] == ("prepare", worker_id, "user_2")
    assert (await store.get(worker_id)).status == "running"
    assert await again.sandbox.download(["self"]) == {
        "self/AGENTS.md": files.File(b"seed"),
        "self/notes.md": files.File(b"uncommitted"),
    }


async def test_running_thread_sandbox_is_reacquired_without_another_prepare(sdk, daemon):
    sandboxes = sandbox.VercelSandboxProvider()
    name = provider.sandbox_name("hatchery:chat_1")
    chat = provider.ThreadChat("chat_1", "user_1")
    await sandboxes.acquire(name, seed=seed(), chat=chat)
    sdk.boxes[name].status = "running"

    again = await sandboxes.acquire(name, seed=seed(), chat=chat)
    await sandboxes.acquire(name, seed=seed(), chat=provider.ThreadChat("chat_1"))

    assert not again.fresh
    assert [call[0] for call in daemon] == ["provision"]
    await sandboxes.acquire(name, seed=seed(), chat=provider.ThreadChat("chat_1", "user_2"))
    assert daemon[-1] == ("prepare", name.removeprefix("hatchery-"), "user_2")


async def test_thread_sandbox_belongs_to_one_chat(sdk, daemon):
    sandboxes = sandbox.VercelSandboxProvider()
    name = provider.sandbox_name("hatchery:chat_1")
    await sandboxes.acquire(name, seed=seed(), chat=provider.ThreadChat("chat_1"))

    with pytest.raises(ValueError, match="another chat"):
        await sandboxes.acquire(name, seed=seed(), chat=provider.ThreadChat("chat_2"))
    with pytest.raises(ValueError, match="chat that owns it"):
        await sandboxes.acquire(name, seed=seed())


async def test_lost_thread_sandbox_is_rebuilt_fresh_with_the_same_worker(sdk, daemon):
    sandboxes = sandbox.VercelSandboxProvider()
    name = provider.sandbox_name("hatchery:chat_1")
    chat = provider.ThreadChat("chat_1", "user_1")
    first = await sandboxes.acquire(name, seed=seed({"self/AGENTS.md": b"old"}), chat=chat)
    before = await store.get(first.sandbox.name.removeprefix("hatchery-"))
    del sdk.boxes[name]

    rebuilt = await sandboxes.acquire(name, seed=seed({"self/AGENTS.md": b"latest"}), chat=chat)

    after = await store.get(before.id)
    assert rebuilt.fresh
    assert [call[0] for call in daemon] == ["provision", "provision"]
    assert (after.daemon_token, after.created_at) == (before.daemon_token, before.created_at)
    assert sdk.boxes[name].workspace == {"self/AGENTS.md": files.File(b"latest")}


async def test_failed_thread_provision_is_recorded_and_retried(sdk, daemon, monkeypatch):
    sandboxes = sandbox.VercelSandboxProvider()
    name = provider.sandbox_name("hatchery:chat_1")
    chat = provider.ThreadChat("chat_1")
    working = sandbox.provision

    async def broken(*args, **kwargs):
        raise RuntimeError("daemon did not start")

    monkeypatch.setattr(sandbox, "provision", broken)
    with pytest.raises(RuntimeError, match="daemon did not start"):
        await sandboxes.acquire(name, seed=seed(), chat=chat)
    assert (await store.get(name.removeprefix("hatchery-"))).status == "failed"

    monkeypatch.setattr(sandbox, "provision", working)
    assert (await sandboxes.acquire(name, seed=seed(), chat=chat)).fresh
    assert (await store.get(name.removeprefix("hatchery-"))).status == "running"


async def test_destroying_a_thread_sandbox_removes_its_worker_tasks_and_terminals(sdk, daemon):
    sandboxes = sandbox.VercelSandboxProvider()
    name = provider.sandbox_name("hatchery:chat_1")
    chat = provider.ThreadChat("chat_1")
    acquired = await sandboxes.acquire(name, seed=seed(), chat=chat)
    worker_id = name.removeprefix("hatchery-")
    await store.save_task(
        models.Task(
            id="task_1",
            chat_id="chat_1",
            worker_id=worker_id,
            title="fx",
            prompt="fix it",
            model="m",
            created_at="now",
            updated_at="now",
        )
    )
    await store.save_terminal(
        models.Terminal(
            id="terminal_1",
            chat_id="chat_1",
            worker_id=worker_id,
            title="bash 1",
            created_at="now",
            updated_at="now",
        )
    )

    await sandboxes.release(acquired.sandbox, keep=False)

    assert sdk.boxes[name].destroyed
    assert await store.get(worker_id) is None
    assert await store.list_tasks("chat_1") == []
    assert await store.list_terminals("chat_1") == []
    await sandboxes.destroy(name)  # idempotent once the record is gone

    again = await sandboxes.acquire(name, seed=seed(), chat=chat)
    await sandboxes.destroy(name)
    assert again.fresh and await store.get(worker_id) is None


async def test_thread_provision_clones_repos_best_effort_without_github(monkeypatch):
    calls = {"runs": [], "mkdir": [], "urls": []}

    class Files:
        async def mkdir(self, path):
            calls["mkdir"].append(path)

        async def write_text(self, path, text, mode):
            pass

    class Process:
        returncode = None

        async def refresh(self):
            pass

    class Box:
        fs = Files()
        region = "iad1"
        routes = [
            types.SimpleNamespace(port=8787, url="https://daemon.example"),
            types.SimpleNamespace(port=8788, url="https://ssh.example"),
        ]

        async def update(self, **options):
            return self

        async def update_network_policy(self, policy):
            calls["network_policy"] = policy

        async def run_process(self, command, args, **options):
            calls["runs"].append((command, args, options))

        async def create_process(self, command, args, env):
            calls["process"] = args[1]
            return Process()

    async def get_or_create_sandbox(**options):
        calls["options"] = options
        return Box(), True

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "version": sandbox.daemon_main.VERSION, "queue_connected": True}

    class Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers):
            calls["urls"].append(url)
            return Response()

    async def oidc_token():
        return "oidc-token"

    async def no_github(user_id, installation_id=None):
        raise sandbox.connections.ConnectionRequired("connect GitHub")

    async def no_identity(user_id):
        return None

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_or_create_sandbox", get_or_create_sandbox)
    monkeypatch.setattr(sandbox.vercel_oidc, "get_vercel_oidc_token", oidc_token)
    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sandbox.connections, "github_token", no_github)
    monkeypatch.setattr(sandbox.connections, "github_identity", no_identity)
    spec = models.WorkerSpec(repos=["acme/app", "acme/private"], purpose="thread")

    provisioned = await sandbox.provision("abc", spec, "secret", user_id="user_1")

    assert provisioned.sandbox_name == "hatchery-abc"
    assert calls["options"]["source"] is None
    assert calls["network_policy"].allow["api.github.com"] == ()
    assert "/workspace/repos" in calls["mkdir"]
    clones = [run for run in calls["runs"] if run[1][0] == "clone"]
    assert clones == [
        (
            "git",
            ["clone", "https://github.com/acme/app.git", "/workspace/repos/app"],
            {"capture_output": True},
        ),
        (
            "git",
            ["clone", "https://github.com/acme/private.git", "/workspace/repos/private"],
            {"capture_output": True},
        ),
    ]
    assert "--workspace /workspace/repos " in calls["process"]
    assert calls["urls"] == ["https://daemon.example/health"], "no repo canonicalization"


async def test_prepare_for_command_does_not_require_github_for_thread_sandboxes(monkeypatch):
    calls = []

    class Box:
        region = "iad1"
        routes = [types.SimpleNamespace(port=8787, url="https://daemon.example")]

        async def update(self, **options):
            return self

        async def update_network_policy(self, policy):
            pass

        async def run_process(self, command, args, **options):
            calls.append(args)

    async def resume_sandbox(name):
        return Box()

    async def no_github(user_id, installation_id=None):
        raise sandbox.connections.ConnectionRequired("connect GitHub")

    async def no_identity(user_id):
        return None

    async def network_policy(credential, region):
        assert credential is None
        return "policy"

    async def repair(box, worker_id, spec, token, routes):
        calls.append("repair")

    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(sandbox.connections, "github_token", no_github)
    monkeypatch.setattr(sandbox.connections, "github_identity", no_identity)
    monkeypatch.setattr(sandbox, "_network_policy", network_policy)
    monkeypatch.setattr(sandbox, "repair_daemon", repair)
    record = models.Worker(
        id="abc",
        chat_id="chat_1",
        sandbox_name="hatchery-abc",
        command_topic="topic",
        title="thread",
        status="stopped",
        spec=models.WorkerSpec(repos=["acme/app"], purpose="thread"),
        daemon_token="secret",
        created_at="now",
        updated_at="now",
    )

    await sandbox.prepare_for_command(record, actor_user_id="user_without_github")

    assert calls[-1] == "repair"
    assert not any("remote" in call for call in calls if isinstance(call, list))
