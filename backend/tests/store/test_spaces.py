import models
from store import spaces


async def test_default_is_created_once():
    first = await spaces.default()
    second = await spaces.default()
    assert first.id == second.id == spaces.DEFAULT_ID
    assert [s.id for s in await spaces.list_all()] == [spaces.DEFAULT_ID]


async def test_save_get_list():
    await spaces.default()
    extra = models.Space(
        id="spc_x", name="x", goal="g", color="#fff", created_at="2099-01-01T00:00:00+00:00"
    )
    await spaces.save(extra)
    loaded = await spaces.get("spc_x")
    assert loaded is not None and loaded.name == "x"
    assert [s.id for s in await spaces.list_all()] == [spaces.DEFAULT_ID, "spc_x"]
    assert await spaces.get("spc_missing") is None
