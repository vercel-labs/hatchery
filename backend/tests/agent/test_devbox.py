import urllib.parse

from agent import devbox


def test_vercel_dev_does_not_register_public_webhook(monkeypatch):
    monkeypatch.delenv("DEVBOX_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    monkeypatch.setenv("VERCEL_URL", "localhost:3000")
    assert devbox.webhook_url() is None


def test_deployment_uses_vercel_webhook(monkeypatch):
    monkeypatch.delenv("DEVBOX_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_URL", "fabricator-preview.vercel.app")
    assert devbox.webhook_url() == "https://fabricator-preview.vercel.app/channels/v1/devbox"


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
