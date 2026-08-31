import types

import httpx

from worker import models, sandbox


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

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_or_create_sandbox", get_or_create_sandbox)
    monkeypatch.setattr(sandbox.vercel_oidc, "get_vercel_oidc_token", oidc_token)
    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)

    provisioned = await sandbox.provision("wrk_1", models.WorkerSpec(), "secret")

    assert provisioned.sandbox_name == "hatchery-wrk_1"
    assert calls["options"]["persistent"] is True
    assert calls["options"]["execution_time_limit"] == sandbox.EXECUTION_TIME_LIMIT
    assert calls["update"] == {"execution_time_limit": sandbox.EXECUTION_TIME_LIMIT}
    bootstrap = calls["runs"][0][1][1]
    for package in ("vercel-queue", "vercel-connect", "asyncssh", "websockets"):
        assert package in bootstrap
    assert "from vercel import connect, queue; import asyncssh, websockets" in bootstrap
    assert "fx.sh/setup.sh" in bootstrap
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


def test_workspace_matches_vercel_git_source_layout():
    assert sandbox._workspace(models.WorkerSpec(repos=["acme/app"])) == "/vercel/app"
    assert sandbox._workspace(models.WorkerSpec()) == "/vercel"


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

    health = iter([
        httpx.ConnectError("down"),
        {
            "ok": True,
            "version": sandbox.daemon_main.VERSION,
            "queue_connected": True,
        },
    ])

    async def daemon_health(url, token):
        result = next(health)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sandbox, "_daemon_health", daemon_health)
    monkeypatch.setattr(sandbox, "_wait_for_daemon", lambda *args, **kwargs: daemon_health(args[0], args[1]))

    await sandbox.repair_daemon(
        Box(), "wrk_1", models.WorkerSpec(), "secret",
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
        Box(), "wrk_1", models.WorkerSpec(), "secret",
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

    async def repair(box, worker_id, spec, token, routes):
        calls.append(("repair", worker_id, token, routes))

    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(sandbox.git, "git_credentials", credentials)
    monkeypatch.setattr(sandbox, "_network_policy", network_policy)
    monkeypatch.setattr(sandbox, "repair_daemon", repair)
    record = models.Worker(
        id="wrk_1", chat_id="chat_1", sandbox_name="hatchery-wrk_1",
        command_topic="topic", title="worker", status="running",
        spec=models.WorkerSpec(), daemon_token="secret",
        created_at="now", updated_at="now",
    )

    await sandbox.prepare_for_command(record)

    assert calls == [
        ("resume", "hatchery-wrk_1"),
        ("update", {"execution_time_limit": sandbox.EXECUTION_TIME_LIMIT}),
        ("policy", "queue-policy"),
        ("repair", "wrk_1", "secret", [models.Route(port=8787, url="https://daemon.example")]),
    ]


async def test_probe_route_rejects_undeclared_port():
    record = models.Worker(
        id="wrk_1", chat_id="chat_1", sandbox_name="hatchery-wrk_1",
        command_topic="topic", title="worker", status="running",
        spec=models.WorkerSpec(ports=[3000]),
        routes=[models.Route(port=3000, url="https://app.example")],
        daemon_token="secret", created_at="now", updated_at="now",
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

    async def configure(found):
        calls.append("git")

    async def credentials():
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
    monkeypatch.setattr(sandbox, "_network_policy", network_policy)
    monkeypatch.setattr(sandbox, "repair_daemon", repair)
    record = models.Worker(
        id="wrk_1", chat_id="chat_1", sandbox_name="hatchery-wrk_1",
        command_topic="topic", title="worker", status="running",
        spec=models.WorkerSpec(),
        routes=[models.Route(port=8787, url="https://daemon.example")],
        daemon_token="secret",
        created_at="now", updated_at="now",
    )

    assert await sandbox.snapshot(record) == "snap_1"
    assert await sandbox.snapshot(record, "snap_1") == "snap_1"
    assert calls == ["snapshot", "stop", ("update", {"current_snapshot_id": "snap_1"}), "resume", "policy", "git", "repair"]


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

    env = sandbox._daemon_env(
        "wrk_1", models.WorkerSpec(), "secret", region="sfo1"
    )

    assert "VERCEL_OIDC_TOKEN" not in env
    assert "VERCEL_DEPLOYMENT_ID" not in env
    assert env["HATCHERY_EVENT_DEPLOYMENT"] == "dpl_1"
    assert env["VERCEL_QUEUE_TOKEN"] == sandbox.QUEUE_TOKEN_PLACEHOLDER
    assert env["VERCEL_REGION"] == "iad1"
    assert env["VERCEL_QUEUE_BASE_URL"] == "https://queues.example"


def test_daemon_env_uses_sandbox_region_when_runtime_region_is_missing(monkeypatch):
    monkeypatch.delenv("VERCEL_REGION", raising=False)

    env = sandbox._daemon_env(
        "wrk_1", models.WorkerSpec(), "secret", region="iad1"
    )

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
