import pytest


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    """Every test gets a fresh file-backed store (never a real database)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HATCHERY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"
