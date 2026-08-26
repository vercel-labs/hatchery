import json
import urllib.parse

import httpx

from agent import devbox


def test_cloud_error_logging_redacts_credentials():
    error = "callback=https://app.test/hook?launch_id=1&secret=hunter2 token=also-secret"
    assert devbox._redact(error) == (
        "callback=https://app.test/hook?launch_id=1&secret=[redacted] token=[redacted]"
    )


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


async def test_create_box_uses_space_authority(monkeypatch):
    seen = {}

    async def request(http, method, url, operation, **kwargs):
        seen.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            content=json.dumps({"id": "box_1", "url": "https://box.example"}).encode(),
        )

    monkeypatch.setattr(devbox, "_request", request)
    auth = devbox.Auth("owner-token", "team_1", "project_1", "owner/repo")

    await devbox.create_box(auth, "hatchery-chat_1")

    assert seen["params"] == {"teamId": "team_1"}
    assert seen["headers"] == {"Authorization": "Bearer owner-token"}
    assert seen["json"] == {
        "name": "hatchery-chat_1",
        "setup": True,
        "sandbox": {},
        "projectId": "project_1",
        "cloneRepos": ["owner/repo"],
    }


def test_tty_url_targets_real_devbox_session():
    auth = devbox.Auth("token", "team_1", "project_1", "owner/repo")
    url = devbox.tty_url(auth, "https://box.example", "session_1", "12", "80", "24")
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
