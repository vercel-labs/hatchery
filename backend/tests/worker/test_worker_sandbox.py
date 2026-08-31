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
        routes = [
            types.SimpleNamespace(port=8787, url="https://daemon.example"),
            types.SimpleNamespace(port=8788, url="https://ssh.example"),
        ]

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
            return {"ok": True, "version": 4}

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

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_or_create_sandbox", get_or_create_sandbox)
    monkeypatch.setattr(sandbox.httpx, "AsyncClient", Client)

    provisioned = await sandbox.provision("wrk_1", models.WorkerSpec(), "secret")

    assert provisioned.sandbox_name == "hatchery-wrk_1"
    assert calls["options"]["persistent"] is True
    bootstrap = calls["runs"][0][1][1]
    for package in ("vercel-queue", "vercel-connect", "asyncssh", "websockets"):
        assert package in bootstrap
    assert "from vercel import connect, queue; import asyncssh, websockets" in bootstrap
    assert "fx.sh/setup.sh" in bootstrap
    assert calls["options"]["ports"] == [8787, 8788]
    assert calls["process"][2]["HATCHERY_DAEMON_TOKEN"] == "secret"
    assert calls["process"][2]["HATCHERY_WORKER_ID"] == "wrk_1"
    assert calls["process"][2]["FX_PERMISSION_MODE"] == "yolo"
    assert calls["health"] == (
        "https://daemon.example/health",
        {"authorization": "Bearer secret"},
    )


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
            return {"ok": True, "version": 4}

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

    assert health == {"ok": True, "version": 4}
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
        routes = [types.SimpleNamespace(port=8787, url="https://daemon.example")]

        async def create_process(self, command, args, env):
            calls["process"] = (command, args, env)
            return Process()

    health = iter([httpx.ConnectError("down"), {"ok": True, "version": 4}])

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
    assert calls["process"][0] == "python3"


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

    async def repair(*args, **kwargs):
        calls.append("repair")

    monkeypatch.setattr(sandbox.vercel_sandbox, "get_sandbox", get_sandbox)
    monkeypatch.setattr(sandbox.vercel_sandbox, "resume_sandbox", resume_sandbox)
    monkeypatch.setattr(sandbox.git, "configure", configure)
    monkeypatch.setattr(sandbox.git, "git_credentials", credentials)
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


def test_daemon_env_preserves_cloud_queue_identity(monkeypatch):
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc")
    monkeypatch.setenv("VERCEL_REGION", "iad1")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_1")
    monkeypatch.setenv("VERCEL_QUEUE_BASE_URL", "https://queues.example")
    monkeypatch.delenv("VERCEL_QUEUE_TOKEN", raising=False)

    env = sandbox._daemon_env("wrk_1", models.WorkerSpec(), "secret")

    assert env["VERCEL_OIDC_TOKEN"] == "oidc"
    assert env["VERCEL_REGION"] == "iad1"
    assert env["VERCEL_DEPLOYMENT_ID"] == "dpl_1"
    assert env["VERCEL_QUEUE_BASE_URL"] == "https://queues.example"


def test_daemon_env_requires_public_origin_for_vercel_dev(monkeypatch):
    monkeypatch.setenv("VERCEL_QUEUE_TOKEN", "vc-dev-token")
    monkeypatch.delenv("HATCHERY_PUBLIC_URL", raising=False)

    try:
        sandbox._daemon_env("wrk_1", models.WorkerSpec(), "secret")
    except RuntimeError as error:
        assert "HATCHERY_PUBLIC_URL" in str(error)
    else:
        raise AssertionError("missing public origin should fail")
