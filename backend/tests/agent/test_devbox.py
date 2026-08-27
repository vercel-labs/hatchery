import json
import urllib.parse

import httpx

from agent import devbox


def test_vercel_dev_does_not_register_public_webhook(monkeypatch):
    monkeypatch.delenv("DEVBOX_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    monkeypatch.setenv("VERCEL_URL", "localhost:3000")
    assert devbox.webhook_url() is None


def test_deployment_uses_vercel_webhook(monkeypatch):
    monkeypatch.delenv("DEVBOX_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_URL", "hatchery-preview.vercel.app")
    assert devbox.webhook_url() == "https://hatchery-preview.vercel.app/channels/v1/devbox"


async def test_create_box_sends_clone_repos(monkeypatch):
    seen = {}

    async def send(self, request, **kwargs):
        seen["request"] = request
        return httpx.Response(
            200,
            request=request,
            json={"id": "box_1", "url": "https://box.example"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(devbox, "token", lambda: "token")
    monkeypatch.setattr(devbox, "_team", lambda: "team_1")

    box = await devbox.create_box("hatchery-chat", ["anbuzin/hatchery"])

    request = seen["request"]
    assert box == {"id": "box_1", "url": "https://box.example"}
    assert request.url.params["teamId"] == "team_1"
    assert json.loads(request.content) == {
        "name": "hatchery-chat",
        "setup": True,
        "sandbox": {},
        "cloneRepos": ["anbuzin/hatchery"],
    }


async def test_create_box_sends_ports_and_main_repo_checkout(monkeypatch):
    seen = {}

    async def send(self, request, **kwargs):
        seen["request"] = request
        return httpx.Response(
            200,
            request=request,
            json={"id": "box_1", "url": "https://box.example"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(devbox, "token", lambda: "token")
    monkeypatch.setattr(devbox, "_team", lambda: "team_1")

    await devbox.create_box(
        "hatchery-chat",
        ["anbuzin/hatchery", "vercel/vercel-py"],
        ports=[3000, 8000],
        branch="feature/api",
        git_sha="abc123",
    )

    assert json.loads(seen["request"].content) == {
        "name": "hatchery-chat",
        "setup": True,
        "sandbox": {"ports": [3000, 8000]},
        "cloneRepos": ["anbuzin/hatchery", "vercel/vercel-py"],
        "branch": "feature/api",
        "gitSha": "abc123",
    }


async def test_create_box_sends_setup_script_as_post_create_command(monkeypatch):
    seen = {}

    async def send(self, request, **kwargs):
        seen["request"] = request
        return httpx.Response(
            200,
            request=request,
            json={"id": "box_1", "url": "https://box.example"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(devbox, "token", lambda: "token")
    monkeypatch.setattr(devbox, "_team", lambda: "team_1")

    await devbox.create_box(
        "hatchery-chat",
        ["anbuzin/hatchery"],
        "curl -LsSf https://astral.sh/uv/install.sh | sh\nfoo bar",
    )

    assert json.loads(seen["request"].content)["config"] == {
        "run": {
            "postCreateCommand": "curl -LsSf https://astral.sh/uv/install.sh | sh\nfoo bar"
        }
    }


async def test_create_task_uses_fx(monkeypatch):
    seen = {}

    async def send(self, request, **kwargs):
        seen["request"] = request
        return httpx.Response(
            200,
            request=request,
            json={"task_id": "task_1", "session_id": "session_1", "state": "pending"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(devbox, "token", lambda: "token")
    monkeypatch.setattr(devbox, "webhook_url", lambda: None)

    task = await devbox.create_task("box_1", "set_1", "fix it")

    assert task == {"task_id": "task_1", "session_id": "session_1", "state": "pending"}
    assert json.loads(seen["request"].content) == {
        "devbox_id": "box_1",
        "set_id": "set_1",
        "assistant": "fx",
        "model": "openai/gpt-5.6-sol",
        "prompt": "fix it",
    }


async def test_delete_task_and_box_use_control_plane_endpoints(monkeypatch):
    seen = []

    async def send(self, request, **kwargs):
        seen.append(request)
        return httpx.Response(200, request=request, json={"deleted": True})

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(devbox, "token", lambda: "token")

    await devbox.delete_task("task_1")
    await devbox.delete_box("box_1")

    assert [(request.method, request.url.path) for request in seen] == [
        ("DELETE", "/v1/tasks/task_1"),
        ("DELETE", "/v1/devbox/box_1"),
    ]
    assert all(request.headers["authorization"] == "Bearer token" for request in seen)


async def test_send_tty_input_attaches_and_writes(monkeypatch):
    sent = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def recv(self):
            return json.dumps({"type": "handshake", "body": {"sessionId": "session_1"}})

        async def send(self, frame):
            sent.append(json.loads(frame))

    async def no_sleep(delay):
        assert delay == 0.75

    monkeypatch.setattr(devbox.websockets, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(devbox, "token", lambda: "token")
    monkeypatch.setattr(devbox.asyncio, "sleep", no_sleep)

    await devbox.send_tty_input(
        "https://box.example", "session_1", b"\x03", b"exit\r"
    )

    assert sent == [
        {"type": "tty-input", "body": {"data": "Aw=="}},
        {"type": "tty-input", "body": {"data": "ZXhpdA0="}},
    ]


async def test_send_task_prompt_does_not_retry(monkeypatch):
    seen = []

    async def send(self, request, **kwargs):
        seen.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"task_id": "task_1", "state": "complete"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(devbox, "token", lambda: "token")

    result = await devbox.send_task_prompt("task_1", "use the existing helper")

    assert result == {"task_id": "task_1", "state": "complete"}
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1/tasks/task_1/prompt"
    assert json.loads(seen[0].content) == {"prompt": "use the existing helper"}


def test_tty_url_starts_manual_session_without_resume_parameters(monkeypatch):
    monkeypatch.setattr(devbox, "token", lambda: "token")
    url = devbox.tty_url("https://box.example", None, "12", "80", "24")
    parsed = urllib.parse.urlsplit(url)
    assert urllib.parse.parse_qs(parsed.query) == {
        "token": ["token"],
        "cols": ["80"],
        "rows": ["24"],
    }


def test_tty_url_targets_real_devbox_session(monkeypatch):
    monkeypatch.setattr(devbox, "token", lambda: "token")
    url = devbox.tty_url("https://box.example", "session_1", "12", "80", "24")
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "wss"
    assert parsed.path == "/__tty"
    assert urllib.parse.parse_qs(parsed.query) == {
        "token": ["token"],
        "sessionId": ["session_1"],
        "offset": ["12"],
        "cols": ["80"],
        "rows": ["24"],
    }
