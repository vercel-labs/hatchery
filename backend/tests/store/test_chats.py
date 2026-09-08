import asyncio
import json

import pytest

import store
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
    assert binding.state == {"user_id": "U1"}


async def test_matching_connected_owner_claims_and_migrates_legacy_binding():
    legacy, _ = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "legacy",
        {"team_id": "T1", "user_id": "U1"},
    )

    claimed, created = await chats.claim(
        "slack:T1:C1:100.1",
        "slack",
        None,
        "connected",
        {"team_id": "T1", "user_id": "U1"},
        user_id="hatchery_1",
        legacy_token="slack:C1:100.1",
    )

    assert created is False
    assert claimed.id == legacy.id
    assert claimed.user_id == "hatchery_1"
    assert (await chats.get(legacy.id)).user_id == "hatchery_1"
    [binding] = await chats.bindings(legacy.id)
    assert binding.token == "slack:T1:C1:100.1"


async def test_matching_connected_owner_snapshots_author_on_claim():
    legacy, _ = await chats.claim(
        "slack:T1:C1:100.2",
        "slack",
        None,
        "legacy",
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
    assert claimed.id == legacy.id
    assert claimed.user_id == "hatchery_1"
    assert claimed.author_display_name == "Ada"


async def test_different_identity_cannot_claim_or_migrate_legacy_binding():
    legacy, _ = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "legacy",
        {"team_id": "T1", "user_id": "U1"},
    )

    rejected, created = await chats.claim(
        "slack:T1:C1:100.1",
        "slack",
        None,
        "takeover",
        {"team_id": "T1", "user_id": "U2"},
        user_id="hatchery_2",
        legacy_token="slack:C1:100.1",
    )

    assert created is False
    assert rejected.id == legacy.id
    assert rejected.user_id is None
    [binding] = await chats.bindings(legacy.id)
    assert binding.token == "slack:C1:100.1"
    assert binding.state == {"team_id": "T1", "user_id": "U1"}


async def test_other_workspace_ignores_legacy_collision_and_creates_scoped_binding():
    legacy, _ = await chats.claim(
        "slack:C1:100.1",
        "slack",
        None,
        "legacy",
        {"team_id": "T1", "user_id": "U1"},
    )

    scoped, created = await chats.claim(
        "slack:T2:C1:100.1",
        "slack",
        None,
        "other workspace",
        {"team_id": "T2", "user_id": "U2"},
        user_id="hatchery_2",
        legacy_token="slack:C1:100.1",
    )

    assert created is True
    assert scoped.id != legacy.id
    assert scoped.user_id == "hatchery_2"
    assert {binding.token for binding in await chats.bindings(legacy.id)} == {
        "slack:C1:100.1"
    }
    assert {binding.token for binding in await chats.bindings(scoped.id)} == {
        "slack:T2:C1:100.1"
    }


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
    chat = await chats.create(
        None, "manual chat", user_id="user_1", author_display_name="Ada"
    )
    assert chat.trigger == "ui"
    assert chat.user_id == "user_1"
    assert chat.author_display_name == "Ada"
    assert chat.space_id is None
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


async def test_bind_is_idempotent_and_exact_without_transferring():
    chat = await chats.create(None, "owner", user_id="owner")
    other = await chats.create(None, "other", user_id="other")
    token = "slack:T1:C1:100.1"
    first = await chats.bind(token, chat.id, "slack", {"user_id": "U1", "comment_id": 1})
    assert await chats.binding(token) == first
    assert await chats.binding("slack:C1:100.1") is None
    assert await chats.bind(token, chat.id, "slack", first.state) == first
    enriched = await chats.bind(token, chat.id, "slack", {"user_id": "U2", "sharing_id": "share1"})
    assert enriched.state == {"user_id": "U1", "comment_id": 1, "sharing_id": "share1"}
    with pytest.raises(ValueError, match="already bound"):
        await chats.bind(token, other.id, "slack", {"user_id": "U2"})
    assert await chats.binding(token) == enriched
    assert (await chats.get(chat.id)).user_id == "owner"
    with pytest.raises(ValueError, match="does not exist"):
        await chats.bind("new", "missing", "slack", {})
    assert await chats.binding("new") is None


async def test_bind_concurrent_chats_have_exactly_one_winner():
    owners = [await chats.create(None, str(i), user_id=str(i)) for i in range(10)]
    results = await asyncio.gather(
        *(chats.bind("github:repo:1:issue:7", owner.id, "github", {}) for owner in owners),
        return_exceptions=True,
    )
    winners = [result for result in results if isinstance(result, chats.Binding)]
    assert len(winners) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 9
    assert await chats.binding("github:repo:1:issue:7") == winners[0]


@pytest.mark.parametrize("allow_participants", [False, True])
async def test_shared_claim_preserves_owner_identity_and_sharing_state(allow_participants):
    owner = await chats.create(None, "shared", user_id="owner", author_display_name="Owner")
    token = "slack:T1:C1:100.1"
    state = {"team_id": "T1", "channel_id": "C1", "thread_ts": "100.1", "user_id": "U1",
             "sharing_id": "share1", "excluded_message_ids": ["private"], "comment_id": 1}
    await chats.bind(token, owner.id, "slack", state)
    claimed, created = await chats.claim(
        token, "slack", None, "participant", {**state, "user_id": "U2", "comment_id": 2,
                                               "sharing_id": "other", "excluded_message_ids": []},
        user_id="participant", author_display_name="Participant", allow_participants=allow_participants,
    )
    assert not created
    assert claimed == owner
    assert await chats.get(owner.id) == owner
    bound = await chats.binding(token)
    assert bound.state == {**state, "comment_id": 2 if allow_participants else 1}


@pytest.mark.parametrize("user_id", [None, "participant"])
async def test_shared_claim_cannot_take_over_unowned_legacy_chat(user_id):
    token = "slack:T1:C1:100.1"
    state = {"team_id": "T1", "user_id": "U1"}
    legacy, _ = await chats.claim(token, "slack", None, "legacy", state)
    rejected, created = await chats.claim(
        token, "slack", None, "participant", {"team_id": "T1", "user_id": "U2"},
        user_id=user_id, allow_participants=True,
    )
    assert not created and rejected == legacy
    assert rejected.user_id is None
    assert (await chats.binding(token)).state == state


async def test_shared_github_participant_updates_comment_not_owner_or_routing():
    owner = await chats.create(None, "shared", user_id="owner")
    token = "github:repo:1:issue:7"
    state = {"owner": "acme", "repo": "hatchery", "repository_id": 1, "kind": "issue",
             "number": 7, "comment_id": 1, "sender_id": "42", "sharing_id": "share1",
             "excluded_message_ids": ["private"], "start_message_id": "1"}
    await chats.bind(token, owner.id, "github", state)
    claimed, created = await chats.claim(
        token, "github", None, "participant", {**state, "comment_id": 2, "sender_id": "43", "number": 8,
                                               "start_message_id": "2"},
        user_id="participant", allow_participants=True,
    )
    assert not created and claimed == owner
    assert (await chats.binding(token)).state == {**state, "comment_id": 2, "sender_id": "43"}
