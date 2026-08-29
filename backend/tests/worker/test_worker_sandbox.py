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
    assert "vercel-queue==0.7.3" in calls["runs"][0][1][1]
    assert "fx.sh/setup.sh" in calls["runs"][0][1][1]
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
