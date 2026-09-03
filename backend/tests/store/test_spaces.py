import pytest
import pydantic

import models
from store import spaces


async def test_default_is_created_once():
    first = await spaces.default()
    second = await spaces.default()
    assert first.id == second.id == spaces.DEFAULT_ID
    assert first.name == "workspace"
    assert first.repos == []
    assert [s.id for s in await spaces.list_all()] == [spaces.DEFAULT_ID]


async def test_save_get_list():
    await spaces.default()
    extra = models.Space(
        id="spc_x", name="x", color="#fff", created_at="2099-01-01T00:00:00+00:00"
    )
    await spaces.save(extra)
    loaded = await spaces.get("spc_x")
    assert loaded is not None and loaded.name == "x"
    assert [s.id for s in await spaces.list_all()] == [spaces.DEFAULT_ID, "spc_x"]
    assert await spaces.get("spc_missing") is None


async def test_create_and_delete():
    created = await spaces.create("new space")

    assert created.id.startswith("spc_")
    assert created.name == "new space"
    assert await spaces.delete(created.id)
    assert await spaces.get(created.id) is None
    assert not await spaces.delete(created.id)


def test_space_repos_require_owner_repo_form():
    with pytest.raises(pydantic.ValidationError, match="owner/repo"):
        models.Space(
            id="spc_x",
            name="x",
            repos=["https://github.com/acme/app"],
            color="#fff",
            created_at="2099-01-01T00:00:00+00:00",
        )
