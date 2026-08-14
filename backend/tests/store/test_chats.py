import asyncio

from store import chats, spaces


async def test_claim_creates_then_reuses():
    space = await spaces.default()
    first, created_first = await chats.claim(
        "slack:C1:100.1", "slack", space.id, "hello", {"channel_id": "C1"}
    )
    second, created_second = await chats.claim(
        "slack:C1:100.1", "slack", space.id, "other", {"user_id": "U2"}
    )
    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    assert second.title == "hello"  # first claim names the chat
    assert second.trigger == "slack:C1:100.1"
    [binding] = await chats.bindings(first.id)
    assert binding.state == {"channel_id": "C1", "user_id": "U2"}  # merged


async def test_claim_separates_tokens():
    space = await spaces.default()
    a, _ = await chats.claim("slack:C1:100.1", "slack", space.id, "t", {})
    b, _ = await chats.claim("slack:C1:200.2", "slack", space.id, "t", {})
    c, _ = await chats.claim("github:repo:1:issue:1", "github", space.id, "t", {})
    assert len({a.id, b.id, c.id}) == 3


async def test_claim_is_single_owner_under_concurrency():
    space = await spaces.default()
    results = await asyncio.gather(
        *(chats.claim("slack:C1:1.0", "slack", space.id, "t", {}) for _ in range(20))
    )
    assert len({chat.id for chat, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_create_get_list():
    space = await spaces.default()
    chat = await chats.create(space.id, "manual chat")
    assert chat.trigger == "ui"
    assert [c.id for c in await chats.list_all()] == [chat.id]
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.title == "manual chat"
    assert await chats.get("chat_missing") is None


async def test_dedupe():
    assert await chats.dedupe("slack:ev1") is True
    assert await chats.dedupe("slack:ev1") is False
    assert await chats.dedupe("slack:ev2") is True
