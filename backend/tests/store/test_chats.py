import asyncio

from store import chats, projects


async def test_claim_creates_then_reuses():
    project = await projects.get_default()
    first, created_first = await chats.claim("slack:C1:100.1", "slack", project.id, "hello", {"channel_id": "C1"})
    second, created_second = await chats.claim("slack:C1:100.1", "slack", project.id, "other", {"user_id": "U2"})
    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    assert second.title == "hello"  # first claim names the chat
    [binding] = await chats.bindings(first.id)
    assert binding.state == {"channel_id": "C1", "user_id": "U2"}  # merged


async def test_claim_separates_tokens():
    project = await projects.get_default()
    a, _ = await chats.claim("slack:C1:100.1", "slack", project.id, "t", {})
    b, _ = await chats.claim("slack:C1:200.2", "slack", project.id, "t", {})
    c, _ = await chats.claim("github:repo:1:issue:1", "github", project.id, "t", {})
    assert len({a.id, b.id, c.id}) == 3


async def test_claim_is_single_owner_under_concurrency():
    project = await projects.get_default()
    results = await asyncio.gather(
        *(chats.claim("slack:C1:1.0", "slack", project.id, "t", {}) for _ in range(20))
    )
    assert len({chat.id for chat, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_create_list_and_status():
    project = await projects.get_default()
    chat = await chats.create(project.id, "manual chat")
    assert [c.id for c in await chats.list_for_project(project.id)] == [chat.id]
    archived = await chats.set_status(chat.id, "archived")
    assert archived is not None and archived.status == "archived"
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.status == "archived"


async def test_get_unknown_returns_none():
    assert await chats.get("cht_missing") is None
    assert await chats.set_status("cht_missing", "archived") is None


async def test_dedupe():
    assert await chats.dedupe("slack:ev1") is True
    assert await chats.dedupe("slack:ev1") is False
    assert await chats.dedupe("slack:ev2") is True
