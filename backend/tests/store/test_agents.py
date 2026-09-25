import pytest
import pydantic

from hatchery import models
from hatchery.store import agents


async def test_default_is_created_once():
    first = await agents.default()
    second = await agents.default()
    assert first.id == second.id == agents.DEFAULT_ID
    assert first.name == "hatchery"
    assert first.repos == []
    assert first.color in agents.ACCENT_COLORS
    assert [s.id for s in await agents.list_all()] == [agents.DEFAULT_ID]


async def test_save_get_list():
    await agents.default()
    extra = models.Agent(
        id="x", name="x", color="pink-900", created_at="2099-01-01T00:00:00+00:00"
    )
    await agents.save(extra)
    loaded = await agents.get("x")
    assert loaded is not None and loaded.name == "x"
    assert [s.id for s in await agents.list_all()] == [agents.DEFAULT_ID, "x"]
    assert await agents.get("missing") is None


async def test_create_and_delete():
    created = await agents.create("New Agent")

    assert created.id == "new-agent"
    assert created.name == "New Agent"
    assert created.color in agents.ACCENT_COLORS
    assert (await agents.create("green", color="green-900")).color == "green-900"
    assert agents.ACCENT_COLORS == (
        "blue-700",
        "red-700",
        "amber-700",
        "green-700",
        "teal-700",
        "purple-700",
        "pink-700",
    )
    assert await agents.delete(created.id)
    assert await agents.get(created.id) is None
    assert not await agents.delete(created.id)


async def test_create_ids():
    assert (await agents.create("Docs", "docs-bot")).id == "docs-bot"
    assert (await agents.create("  Café & Release Notes!  ")).id == "cafe-release-notes"
    assert (await agents.create("x" * 80)).id == "x" * 63

    with pytest.raises(agents.Taken):
        await agents.create("Other docs", "docs-bot")
    with pytest.raises(agents.Taken):
        await agents.create("Docs Bot")
    for bad in ("Docs", "-docs", "docs-", "docs_bot", "", "a" * 64):
        with pytest.raises(ValueError, match="1-63"):
            await agents.create("docs", bad)
    with pytest.raises(ValueError):
        await agents.create("!!!")
    assert (await agents.get("docs-bot")).name == "Docs"


def test_agent_repos_require_owner_repo_form():
    with pytest.raises(pydantic.ValidationError, match="owner/repo"):
        models.Agent(
            id="x",
            name="x",
            repos=["https://github.com/acme/app"],
            color="pink-900",
            created_at="2099-01-01T00:00:00+00:00",
        )
