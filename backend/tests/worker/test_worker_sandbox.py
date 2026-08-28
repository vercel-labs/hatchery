import types

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
        routes = [types.SimpleNamespace(port=8787, url="https://daemon.example")]

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
            return {"ok": True, "version": 2}

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
    assert "fx.sh/setup.sh" in calls["runs"][0][1][1]
    assert calls["options"]["ports"] == [8787]
    assert calls["process"][2]["HATCHERY_DAEMON_TOKEN"] == "secret"
    assert calls["process"][2]["HATCHERY_WORKER_ID"] == "wrk_1"
    assert calls["process"][2]["FX_PERMISSION_MODE"] == "yolo"
    assert calls["health"] == (
        "https://daemon.example/health",
        {"authorization": "Bearer secret"},
    )
