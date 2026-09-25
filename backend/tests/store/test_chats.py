import asyncio
import json

import pytest

from hatchery import store
from hatchery.store import agents, chats


async def test_claim_creates_then_reuses():
    agent = await agents.default()
    first, created_first = await chats.claim(
        "slack:C1:100.1", "slack", agent.id, "hello", {"channel_id": "C1"}
    )
    second, created_second = await chats.claim(
        "slack:C1:100.1", "slack", agent.id, "other", {"user_id": "U2"}
    )
    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    assert second.title == "hello"  # first claim names the chat
    assert second.trigger == "slack:C1:100.1"
    [binding] = await chats.bindings(first.id)
    assert binding.state == {"channel_id": "C1", "user_id": "U2"}  # merged


async def test_bind_attaches_existing_chat_and_updates_state():
    chat = await chats.create(None, "notify")
    first = await chats.bind(
        "slack:T1:C1:1.0", chat.id, "slack", {"channel_id": "C1", "user_id": "U1"}
    )
    second = await chats.bind(
        "slack:T1:C1:1.0", chat.id, "slack", {"user_id": "U2", "message_id": "1.1"}
    )

    assert first.chat_id == chat.id
    assert second.state == {"channel_id": "C1", "user_id": "U2", "message_id": "1.1"}
    assert await chats.binding(first.token) == second


async def test_bind_refuses_to_transfer_external_thread():
    first = await chats.create(None, "first")
    second = await chats.create(None, "second")
    await chats.bind("github:repo:1:issue:7", first.id, "github", {})

    with pytest.raises(ValueError, match="another chat"):
        await chats.bind("github:repo:1:issue:7", second.id, "github", {})


async def test_claim_keeps_creator_while_updating_shared_binding_state():
    first, created = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "hello",
        {"user_id": "U1"},
        user_id="hatchery_1",
        author_display_name="Ada",
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
    assert second.author_display_name == "Ada"
    [binding] = await chats.bindings(first.id)
    assert binding.state == {"user_id": "U2"}


async def test_claiming_a_bound_token_does_not_invent_creator():
    first, _ = await chats.claim(
        "slack:T1:C1:100.2",
        "slack",
        None,
        "first",
        {"team_id": "T1", "user_id": "U1"},
    )

    claimed, created = await chats.claim(
        "slack:T1:C1:100.2",
        "slack",
        None,
        "connected",
        {"team_id": "T1", "user_id": "U1"},
        user_id="hatchery_1",
        author_display_name="Ada",
    )

    assert created is False
    assert claimed.id == first.id
    assert claimed.user_id is None
    assert claimed.author_display_name is None


async def test_claim_separates_tokens():
    agent = await agents.default()
    a, _ = await chats.claim("slack:C1:100.1", "slack", agent.id, "t", {})
    b, _ = await chats.claim("slack:C1:200.2", "slack", agent.id, "t", {})
    c, _ = await chats.claim("github:repo:1:issue:1", "github", agent.id, "t", {})
    assert len({a.id, b.id, c.id}) == 3


async def test_claim_is_single_owner_under_concurrency():
    agent = await agents.default()
    results = await asyncio.gather(
        *(chats.claim("slack:C1:1.0", "slack", agent.id, "t", {}) for _ in range(20))
    )
    assert len({chat.id for chat, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_create_once_is_retry_safe_under_concurrency():
    results = await asyncio.gather(
        *(
            chats.create_once("chat_123456789abc", None, "new chat", user_id="user_1")
            for _ in range(20)
        )
    )

    assert {chat.id for chat in results} == {"chat_123456789abc"}
    assert [chat.id for chat in await chats.list_all()] == ["chat_123456789abc"]


async def test_create_once_rejects_conflicting_retry():
    await chats.create_once("chat_123456789abc", None, "new chat", user_id="user_1")

    with pytest.raises(ValueError, match="conflicts"):
        await chats.create_once(
            "chat_123456789abc", "agent_other", "new chat", user_id="user_1"
        )


async def test_create_get_list():
    chat = await chats.create(
        None, "manual chat", user_id="user_1", author_display_name="Ada"
    )
    assert chat.trigger == "ui"
    assert chat.user_id == "user_1"
    assert chat.author_display_name == "Ada"
    assert chat.agent_id is None
    assert [c.id for c in await chats.list_all()] == [chat.id]
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.title == "manual chat"
    assert await chats.get("chat_missing") is None


async def test_legacy_chat_without_archive_field_loads_as_active():
    chat = await chats.create(None, "legacy")
    path = store.data_dir() / "chats" / f"{chat.id}.json"
    data = json.loads(path.read_text())
    data.pop("archived_at")
    data.pop("author_display_name")
    path.write_text(json.dumps(data))

    loaded = await chats.get(chat.id)

    assert loaded is not None and loaded.archived_at is None
    assert loaded.author_display_name is None


async def test_attention_reason_is_persisted_and_cleared():
    chat = await chats.create(None, "work")

    required = await chats.set_attention(chat.id, "result_available")
    assert required is not None and required.attention_reason == "result_available"
    assert (await chats.get(chat.id)).attention_reason == "result_available"

    cleared = await chats.set_attention(chat.id, None)
    assert cleared is not None and cleared.attention_reason is None
    assert (await chats.get(chat.id)).attention_reason is None


async def test_archive_and_unarchive_are_persisted_and_idempotent():
    chat = await chats.create(None, "work")

    archived = await chats.set_archived(chat.id, True)
    archived_again = await chats.set_archived(chat.id, True)

    assert archived is not None and archived.archived_at is not None
    assert archived_again is not None
    assert archived_again.archived_at == archived.archived_at
    assert (await chats.get(chat.id)).archived_at == archived.archived_at

    unarchived = await chats.set_archived(chat.id, False)
    assert unarchived is not None and unarchived.archived_at is None
    assert (await chats.get(chat.id)).archived_at is None
    assert await chats.set_archived("chat_missing", True) is None


async def test_claim_user_sets_legacy_owner_once():
    chat = await chats.create(None, "legacy")

    claimed = await chats.claim_user(chat.id, "user_1", "Ada")
    unchanged = await chats.claim_user(chat.id, "user_2", "Grace")

    assert claimed is not None and claimed.user_id == "user_1"
    assert claimed.author_display_name == "Ada"
    assert unchanged is not None and unchanged.user_id == "user_1"
    assert unchanged.author_display_name == "Ada"


async def test_assign_agent_updates_chat():
    destination = await agents.create("docs")
    chat = await chats.create(None, "work")

    assigned = await chats.assign_agent(chat.id, destination.id)

    assert assigned is not None and assigned.agent_id == destination.id
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.agent_id == destination.id
    assert await chats.assign_agent("chat_missing", destination.id) is None


async def test_set_topic_updates_chat():
    chat = await chats.create(None, "work")

    named = await chats.set_topic(chat.id, "Improve chat names")

    assert named is not None and named.topic == "Improve chat names"
    loaded = await chats.get(chat.id)
    assert loaded is not None and loaded.topic == "Improve chat names"
    assert await chats.set_topic("chat_missing", "Missing") is None


async def test_finish_updates_status_and_artifact():
    agent = await agents.default()
    chat = await chats.create(agent.id, "work")
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
