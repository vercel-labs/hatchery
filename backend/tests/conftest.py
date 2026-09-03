import pytest


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    """Every test gets a fresh file-backed application store."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HATCHERY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HATCHERY_PUBLIC_URL", "https://hatchery.example")
    monkeypatch.setenv("VERCEL_APP_CLIENT_ID", "test-client")
    monkeypatch.setenv("VERCEL_APP_CLIENT_SECRET", "test-secret")

    async def session_user(_session_id):
        return {"id": "user_test"}

    from app import server

    monkeypatch.setattr(server.auth, "session_user", session_user)
    monkeypatch.setattr(server.auth, "current_user", lambda request: session_user("test-session"))
    return tmp_path / "data"
