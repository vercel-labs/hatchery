import pytest
import pydantic

import models
from store import agents


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HATCHERY_DATA_DIR", str(tmp_path / "data"))


async def test_default_is_created_once():
    first = await agents.default()
    second = await agents.default()

    assert first.id == second.id == agents.DEFAULT_ID
    assert first.slug == "hatchery"
    assert first.name == "hatchery"
    assert first.color in agents.ACCENT_COLORS
    assert [agent.id for agent in await agents.list_all()] == [agents.DEFAULT_ID]


async def test_save_get_list_and_get_by_slug():
    await agents.default()
    extra = models.Agent(
        id="agt_x",
        slug="release-reviewer",
        name="Release reviewer",
        color="#fff",
        created_at="2099-01-01T00:00:00+00:00",
    )
    await agents.save(extra)

    assert (await agents.get("agt_x")).name == "Release reviewer"
    assert (await agents.get_by_slug("release-reviewer")).id == "agt_x"
    assert [agent.id for agent in await agents.list_all()] == [agents.DEFAULT_ID, "agt_x"]
    assert await agents.get("agt_missing") is None


async def test_create_delete_and_immutable_unique_slug():
    created = await agents.create("New agent", "new-agent")

    assert created.id.startswith("agt_")
    assert created.slug == "new-agent"
    assert (await agents.create("Green", "green-agent", "green-900")).color == "green-900"
    changed = created.model_copy(update={"slug": "changed"})
    with pytest.raises(agents.SlugImmutable):
        await agents.save(changed)
    with pytest.raises(agents.SlugExists):
        await agents.save(
            models.Agent(
                id="agt_duplicate",
                slug=created.slug,
                name="Duplicate",
                color="#fff",
                created_at="2099-01-01T00:00:00+00:00",
            )
        )
    assert await agents.delete(created.id)
    assert await agents.get(created.id) is None
    assert not await agents.delete(created.id)


@pytest.mark.parametrize("slug", ["Upper", "two--dashes", "-start", "end-", "has blank", "résumé"])
def test_agent_slug_must_be_lowercase_and_path_safe(slug):
    with pytest.raises(pydantic.ValidationError, match="slug"):
        models.Agent(
            id="agt_x",
            slug=slug,
            name="x",
            color="#fff",
            created_at="2099-01-01T00:00:00+00:00",
        )
