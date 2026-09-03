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


async def test_claim_sets_owner_and_rejects_owner_state_takeover():
    first, created = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "hello",
        {"user_id": "U1"},
        user_id="hatchery_1",
    )
    second, reused = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "takeover",
        {"user_id": "U2"},
        user_id="hatchery_2",
    )

    assert created is True and reused is False
    assert first.id == second.id
    assert second.user_id == "hatchery_1"
    [binding] = await chats.bindings(first.id)
    assert binding.state == {"user_id": "U1"}


async def test_connected_owner_claims_legacy_unowned_binding():
    legacy, _ = await chats.claim(
        "slack:C1:100.1", "slack", None, "legacy", {"user_id": "U1"}
    )

    claimed, created = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "connected",
        {"user_id": "U1"},
        user_id="hatchery_1",
    )

    assert created is False
    assert claimed.id == legacy.id
    assert claimed.user_id == "hatchery_1"
    assert (await chats.get(legacy.id)).user_id == "hatchery_1"


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
    chat = await chats.create(None, "manual chat")
    assert chat.trigger == "ui"
    assert chat.space_id is None
    assert [c.id for c in await chats.list_all()] == [chat.id]
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.title == "manual chat"
    assert await chats.get("chat_missing") is None


async def test_claim_user_sets_legacy_owner_once():
    chat = await chats.create(None, "legacy")

    claimed = await chats.claim_user(chat.id, "user_1")
    unchanged = await chats.claim_user(chat.id, "user_2")

    assert claimed is not None and claimed.user_id == "user_1"
    assert unchanged is not None and unchanged.user_id == "user_1"


async def test_assign_space_updates_chat():
    destination = await spaces.create("docs")
    chat = await chats.create(None, "work")

    assigned = await chats.assign_space(chat.id, destination.id)

    assert assigned is not None and assigned.space_id == destination.id
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.space_id == destination.id
    assert await chats.assign_space("chat_missing", destination.id) is None


async def test_set_topic_updates_chat():
    chat = await chats.create(None, "work")

    named = await chats.set_topic(chat.id, "Improve chat names")

    assert named is not None and named.topic == "Improve chat names"
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.topic == "Improve chat names"
    assert await chats.set_topic("chat_missing", "Missing") is None


async def test_finish_updates_status_and_artifact():
    space = await spaces.default()
    chat = await chats.create(space.id, "work")
    finished = await chats.finish(chat.id, "done", "https://example.com/pr/1")
    assert finished is not None
    assert finished.status == "done"
    assert finished.artifact == "https://example.com/pr/1"
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.status == "done"


async def test_dedupe():
    assert await chats.dedupe("slack:ev1") is True
    assert await chats.dedupe("slack:ev1") is False
    assert await chats.dedupe("slack:ev2") is True
