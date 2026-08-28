import asyncio

import httpx
import pytest

import ai
import channels
from app import server
from store import activity, chats, events, subagents, terminals


def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def generated_topic(monkeypatch):
    async def generate(prompt):
        return "Test request"

    monkeypatch.setattr(server.topic, "generate", generate)


async def test_spaces_seed_default():
    async with client() as c:
        listed = (await c.get("/api/spaces")).json()
    assert [s["id"] for s in listed] == ["spc_hatchery"]
    assert "goal" not in listed[0]
    assert not listed[0]["about"].startswith("# hatchery")


async def test_space_create_and_delete():
    async with client() as c:
        created = await c.post("/api/spaces", json={"name": "  docs  "})
        listed = (await c.get("/api/spaces")).json()
        deleted = await c.delete(f"/api/spaces/{created.json()['id']}")

    assert created.status_code == 200
    assert created.json()["name"] == "docs"
    assert created.json()["about"] == ""
    assert [space["id"] for space in listed] == [created.json()["id"]]
    assert deleted.status_code == 204


async def test_space_delete_rejects_unknown_space_and_space_with_chats():
    space = await server.spaces.create("busy")
    await chats.create(space.id, "chat")

    async with client() as c:
        busy = await c.delete(f"/api/spaces/{space.id}")
        missing = await c.delete("/api/spaces/spc_missing")

    assert busy.status_code == 409
    assert busy.json() == {"detail": "space still has chats"}
    assert missing.status_code == 404


async def test_space_update():
    original = await server.spaces.default()
    async with client() as c:
        response = await c.patch(
            "/api/spaces/spc_hatchery",
            json={"name": "  Hatchery docs  ", "about": "# Overview\n\nEdited directly."},
        )
        listed = (await c.get("/api/spaces")).json()

    assert response.status_code == 200
    assert response.json()["name"] == "Hatchery docs"
    assert response.json()["about"] == "# Overview\n\nEdited directly."
    assert response.json()["repos"] == original.repos
    assert response.json()["resources"] == [resource.model_dump() for resource in original.resources]
    assert response.json()["color"] == original.color
    assert response.json()["created_at"] == original.created_at
    assert listed[0] == response.json()


async def test_space_update_rejects_unknown_space_and_empty_name():
    await server.spaces.default()
    async with client() as c:
        missing = await c.patch(
            "/api/spaces/spc_missing", json={"name": "missing", "about": ""}
        )
        invalid = await c.patch(
            "/api/spaces/spc_hatchery", json={"name": "   ", "about": "body"}
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422


async def test_space_resources_update():
    await server.spaces.default()
    async with client() as c:
        response = await c.patch(
            "/api/spaces/spc_hatchery/resources",
            json={
                "repos": ["anbuzin/hatchery"],
                "resources": [
                    {"title": "docs", "url": "https://example.com/docs", "kind": "link"}
                ],
            },
        )
        listed = (await c.get("/api/spaces")).json()

    assert response.status_code == 200
    assert response.json()["repos"] == ["anbuzin/hatchery"]
    assert response.json()["resources"] == [
        {"title": "docs", "url": "https://example.com/docs", "kind": "link"}
    ]
    assert listed[0]["resources"] == response.json()["resources"]


async def test_space_resources_update_rejects_unknown_space_and_invalid_repo():
    await server.spaces.default()
    async with client() as c:
        missing = await c.patch(
            "/api/spaces/spc_missing/resources", json={"repos": [], "resources": []}
        )
        invalid = await c.patch(
            "/api/spaces/spc_hatchery/resources",
            json={"repos": ["https://github.com/anbuzin/hatchery"], "resources": []},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422


async def test_chat_create_and_list():
    async with client() as c:
        created = (await c.post("/api/chats", json={})).json()
        assert created["space_id"] is None
        assert created["title"] == "new chat"
        listed = (await c.get("/api/chats")).json()
    assert [x["id"] for x in listed] == [created["id"]]


async def test_chat_space_assignment():
    destination = await server.spaces.create("docs")
    chat = await chats.create(None, "work")

    async with client() as c:
        response = await c.patch(
            f"/api/chats/{chat.id}/space", json={"space_id": destination.id}
        )
        listed = (await c.get("/api/chats")).json()

    assert response.status_code == 200
    assert response.json()["space_id"] == destination.id
    assert listed[0]["space_id"] == destination.id
    assert (await server._space_for_chat(chat.id)).id == destination.id


async def test_chat_space_assignment_rejects_unknown_chat_space_and_null():
    destination = await server.spaces.create("docs")
    chat = await chats.create(destination.id, "work")
    async with client() as c:
        missing_chat = await c.patch(
            "/api/chats/chat_missing/space", json={"space_id": destination.id}
        )
        missing_space = await c.patch(
            "/api/chats/chat_missing/space", json={"space_id": "spc_missing"}
        )
        null_space = await c.patch(
            f"/api/chats/{chat.id}/space", json={"space_id": None}
        )

    assert missing_chat.status_code == 404
    assert missing_chat.json() == {"detail": "unknown chat"}
    assert missing_space.status_code == 404
    assert missing_space.json() == {"detail": "unknown space"}
    assert null_space.status_code == 422
    assert (await chats.get(chat.id)).space_id == destination.id


async def test_name_chat_generates_and_persists_topic(monkeypatch):
    chat = await chats.create(None, "new chat")

    async def generate(prompt):
        assert prompt == "Improve chat names"
        return "Sidebar chat names"

    monkeypatch.setattr(server.topic, "generate", generate)
    await server._name_chat(chat.id, "Improve chat names")

    named = await chats.get(chat.id)
    assert named is not None and named.topic == "Sidebar chat names"
    assert await events.read(chat.id, "ui") == [(0, {"type": "chat.changed"})]


async def test_chat_list_cleans_legacy_slack_title():
    space = await server.spaces.default()
    chat, _ = await chats.claim(
        "slack:C1:1.0",
        "slack",
        space.id,
        "<@UBOT> old &lt;-&gt; title",
        {},
    )

    async with client() as c:
        [listed] = (await c.get("/api/chats")).json()

    assert listed["id"] == chat.id
    assert listed["title"] == "slack: old <-> title"


async def test_chat_events_replay_after_cursor():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "events")
    await events.append(chat.id, "ui", {"type": "old"})
    await events.append(chat.id, "ui", {"type": "messages.changed"})

    request = httpx.Request("GET", f"http://test/api/chats/{chat.id}/events")
    response = await server.chat_events(chat.id, request, after=0)
    chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert chunk == 'id: 1\ndata: {"type":"messages.changed"}\n\n'


async def test_chat_messages_from_store():
    for message in (ai.user_message("hi"), ai.assistant_message("hello")):
        await events.append("chat_x", "messages", message.model_dump(mode="json"))
    async with client() as c:
        ui = (await c.get("/api/chats/chat_x/messages")).json()
        empty = (await c.get("/api/chats/chat_empty/messages")).json()
    assert [m["role"] for m in ui] == ["user", "assistant"]
    assert ui[0]["parts"][0]["text"] == "hi"
    assert empty == []


async def test_chat_messages_hide_slack_envelope_and_mark_origin():
    text = (
        '<slack_message channel="C1" thread_ts="1.0" ts="1.1" sender="U1" team="T1">\n'
        "hello &lt;-&gt; slack\n</slack_message>"
    )
    await events.append("chat_x", "messages", ai.user_message(text).model_dump(mode="json"))

    async with client() as c:
        [message] = (await c.get("/api/chats/chat_x/messages")).json()

    assert message["parts"][0]["text"] == "hello <-> slack"
    assert message["metadata"]["origin"] == "slack"
    stored = await events.read("chat_x", "messages")
    assert ai.messages.Message.model_validate(stored[0][1]).text == text


def test_dedupe_tool_history_repairs_old_ui_duplicates():
    call = ai.messages.ToolCallPart(
        tool_call_id="call_1", tool_name="create_subagent", tool_args='{"task":"x"}'
    )
    result = ai.messages.ToolResultPart(
        tool_call_id="call_1", tool_name="create_subagent", result="accepted"
    )
    history = [
        ai.assistant_message(call),
        ai.tool_message(result),
        ai.assistant_message(call.model_copy(update={"id": "part_duplicate"})),
        ai.tool_message(result.model_copy(update={"id": "part_duplicate_result"})),
    ]
    repaired = server._dedupe_tool_history(history)
    assert len(repaired) == 2
    assert repaired[0].tool_calls[0].tool_call_id == "call_1"
    assert repaired[1].tool_results[0].tool_call_id == "call_1"


async def test_hub_lands_inbound_in_one_chat(monkeypatch):
    async def turn(chat_id, record):
        return "reply"

    async def classify(prompt, metadata, candidates):
        return candidates[0]

    delivered = []

    async def deliver(chat_id, message):
        delivered.append((chat_id, message))
        return []

    async def emit(chat_id, event):
        delivered.append((chat_id, event.type))
        return []

    monkeypatch.setattr(server, "_run_dispatcher_turn", turn)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_deliver", deliver)
    monkeypatch.setattr(server, "_emit", emit)

    hub = server.bot.hub
    await hub.dispatch(
        "slack",
        channels.Inbound(token="C1:1.0", text="from slack", state={"channel_id": "C1"}, title="a thread"),
    )
    await hub.dispatch("slack", channels.Inbound(token="C1:1.0", text="again", state={}))
    [chat] = await chats.list_all()
    assert chat.trigger == "slack:C1:1.0"
    assert chat.title == "a thread"
    stored = await events.read(chat.id, "messages")
    assert len(stored) == 2
    assert delivered == [
        (chat.id, channels.protocol.SPACE_ASSIGNING),
        (chat.id, channels.protocol.SPACE_ASSIGNED),
        (chat.id, channels.protocol.TURN_STARTED),
        (chat.id, "reply"),
        (chat.id, channels.protocol.TURN_STARTED),
        (chat.id, "reply"),
    ]


async def test_ambiguous_repo_classifies_then_runs_original_request(monkeypatch):
    first = await server.spaces.create("docs")
    first.repos = ["vercel/repo"]
    await server.spaces.save(first)
    second = await server.spaces.create("release")
    second.repos = ["vercel/repo"]
    await server.spaces.save(second)
    emitted = []
    runs = []
    classified = []

    async def emit(chat_id, event):
        emitted.append((chat_id, event))
        return []

    async def classify(prompt, metadata, candidates):
        classified.append((prompt, metadata, [space.id for space in candidates]))
        return second

    async def run(chat_id):
        runs.append(chat_id)

    monkeypatch.setattr(server, "_emit", emit)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    await server.bot.hub.dispatch(
        "github",
        channels.Inbound(
            token="repo:1:issue:7",
            text="fix the docs",
            state={"kind": "issue", "sender": "octocat"},
            repo="vercel/repo",
        ),
    )

    [chat] = await chats.list_all()
    assert chat.space_id == second.id
    assert classified == [
        (
            "fix the docs",
            {
                "origin": "github",
                "author": "octocat",
                "repo": "vercel/repo",
                "channel_state": {"kind": "issue", "sender": "octocat"},
            },
            [first.id, second.id],
        )
    ]
    assert [event.type for _, event in emitted] == [
        channels.protocol.SPACE_ASSIGNING,
        channels.protocol.SPACE_ASSIGNED,
    ]
    assert emitted[-1][1].data["space"]["name"] == "release"
    assert len(await events.read(chat.id, "messages")) == 1
    assert runs == [chat.id]


async def test_inbound_turn_delivers_failure(monkeypatch):
    delivered = []

    async def turn(chat_id, record):
        raise RuntimeError("gateway unavailable")

    async def emit(chat_id, event):
        delivered.append(event)
        return []

    monkeypatch.setattr(server, "_run_dispatcher_turn", turn)
    monkeypatch.setattr(server, "_emit", emit)
    await server._run_inbound_turn("chat_x")

    assert [event.type for event in delivered] == [
        channels.protocol.TURN_STARTED,
        channels.protocol.TURN_FAILED,
    ]
    assert delivered[-1].data == {"error": "gateway unavailable"}


async def test_hub_dedupe_is_durable():
    hub = server.bot.hub
    assert await hub.dedupe("slack:ev1") is True
    assert await hub.dedupe("slack:ev1") is False


async def test_slack_webhook_runs_dispatcher_and_replies_in_thread(monkeypatch):
    slack_channel = server.bot.channels["slack"]
    delivered = []
    seen = {}

    async def verify(headers):
        return None

    async def turn(chat_id, record):
        seen["chat_id"] = chat_id
        seen["record"] = record
        return "I can help with that."

    async def classify(prompt, metadata, candidates):
        return candidates[0]

    async def on_event(event, state):
        delivered.append((event, state))

    monkeypatch.setattr(server.slack.connect, "verify_connect_webhook", verify)
    monkeypatch.setattr(server, "_run_dispatcher_turn", turn)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(slack_channel, "on_event", on_event)

    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev1",
        "authorizations": [{"user_id": "UBOT"}],
        "event": {
            "type": "app_mention",
            "channel": "C1",
            "ts": "100.1",
            "user": "U1",
            "text": "<@UBOT> inspect this",
        },
    }
    async with client() as c:
        response = await c.post(
            "/channels/v1/slack", headers={"authorization": "Bearer good"}, json=payload
        )

    assert response.status_code == 200
    [chat] = await chats.list_all()
    assert seen == {"chat_id": chat.id, "record": {"id": chat.id}}
    assert [event.type for event, _ in delivered] == [
        channels.protocol.SPACE_ASSIGNING,
        channels.protocol.SPACE_ASSIGNED,
        channels.protocol.TURN_STARTED,
        channels.protocol.MESSAGE_COMPLETED,
    ]
    assert delivered[-1][0].data == {"message": "I can help with that."}
    assert delivered[-1][1]["channel_id"] == "C1"
    assert delivered[-1][1]["thread_ts"] == "100.1"


async def test_first_ui_prompt_classifies_before_dispatcher(monkeypatch):
    docs = await server.spaces.create("docs")
    docs.repos = ["vercel/docs"]
    await server.spaces.save(docs)
    chat = await chats.create(None, "new chat")
    seen = {}

    async def classify(prompt, metadata, candidates):
        seen["classification"] = (prompt, metadata, [space.id for space in candidates])
        return docs

    class FakeRun:
        def __init__(self, history):
            self.messages = [*history, ai.assistant_message("dispatched")]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeAgent:
        def run(self, model, history):
            seen["history"] = history
            return FakeRun(history)

    async def fake_sse(result):
        yield 'data: {"type":"finish"}\n\n'

    monkeypatch.setattr(server.classifier, "classify", classify)

    def agent_for(record):
        return FakeAgent()

    monkeypatch.setattr(server.dispatcher, "agent_for", agent_for)
    monkeypatch.setattr(server.ai.ui.ai_sdk, "to_sse", fake_sse)
    ui = ai.ui.ai_sdk.to_ui_messages([ai.user_message("fix the docs")])
    async with client() as c:
        response = await c.post(
            "/api/chat",
            json={
                "chat_id": chat.id,
                "messages": [message.model_dump(mode="json") for message in ui],
            },
        )

    assert response.status_code == 200
    assert seen["classification"] == (
        "fix the docs",
        {"origin": "ui", "author": "current user"},
        [docs.id],
    )
    assert (await chats.get(chat.id)).space_id == docs.id
    assert '"state": "assigning"' in response.text
    assert '"state": "assigned"' in response.text
    assert response.text.index('"state": "assigned"') < response.text.index('"type":"finish"')
    assert seen["history"][0].role == "system"
    assert seen["history"][1].text == "fix the docs"


async def test_ui_turn_is_mirrored_to_bound_channel(monkeypatch):
    class FakeChannel:
        name = "fake"

        def __init__(self):
            self.delivered = []

        async def on_event(self, event, state):
            self.delivered.append((event, state))

    class FakeRun:
        def __init__(self, history):
            self.messages = [*history, ai.assistant_message("answer from AI")]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeAgent:
        def run(self, model, history):
            return FakeRun(history)

    async def fake_sse(result):
        yield 'data: {"type":"finish"}\n\n'

    channel = FakeChannel()
    previous = server.bot.channels.get("fake")
    server.bot.channels["fake"] = channel
    monkeypatch.setattr(
        server.dispatcher, "agent_for", lambda record: FakeAgent()
    )
    monkeypatch.setattr(server.ai.ui.ai_sdk, "to_sse", fake_sse)
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("fake:thread", "fake", space.id, "thread", {"thread": "1"})
        ui = ai.ui.ai_sdk.to_ui_messages([ai.user_message("continue in UI")])
        async with client() as c:
            response = await c.post(
                "/api/chat",
                json={
                    "chat_id": chat.id,
                    "messages": [message.model_dump(mode="json") for message in ui],
                },
            )

        assert response.status_code == 200
        assert [event.type for event, _ in channel.delivered] == [
            channels.protocol.MESSAGE_RECEIVED,
            channels.protocol.TURN_STARTED,
            channels.protocol.MESSAGE_COMPLETED,
        ]
        assert channel.delivered[0][0].data == {"message": "continue in UI", "origin": "ui"}
        assert channel.delivered[-1][0].data == {"message": "answer from AI"}
        assert all(state == {"thread": "1"} for _, state in channel.delivered)
        stored = [ai.messages.Message.model_validate(data) for _, data in await events.read(chat.id, "messages")]
        assert [(message.role, message.text) for message in stored] == [
            ("user", "continue in UI"),
            ("assistant", "answer from AI"),
        ]
    finally:
        if previous is None:
            server.bot.channels.pop("fake", None)
        else:
            server.bot.channels["fake"] = previous


async def test_devbox_completion_is_persisted_and_delivered_once(monkeypatch):
    class FakeChannel:
        name = "fake"

        def __init__(self):
            self.delivered = []

        async def on_event(self, event, state):
            self.delivered.append((event, state))

    seen_wake = None

    async def fake_turn(chat_id, record, wake):
        nonlocal seen_wake
        seen_wake = wake
        await events.append(
            chat_id, "messages", ai.assistant_message("The coder fixed it.").model_dump(mode="json")
        )
        return server.CompletionOutcome(notify=True, message="The coder fixed it.")

    pending = []
    monkeypatch.setattr(server, "_run_completion_turn", fake_turn)
    monkeypatch.setattr(server, "_spawn", pending.append)
    channel = FakeChannel()
    previous = server.bot.channels.get("fake")
    server.bot.channels["fake"] = channel
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("fake:thread", "fake", space.id, "task", {"thread": "1"})
        launch = await subagents.create(chat.id, "devbox_1", "fix it", "secret")
        launch["task_id"] = "task_1"
        await subagents.save(launch)
        body = {
            "kind": "taskStateChange",
            "taskStateChange": {
                "taskId": "task_1",
                "state": "complete",
                "seq": 4,
                "result": {
                    "summary": "fixed it",
                    "prs": [{"url": "https://github.com/a/b/pull/1"}],
                },
            },
        }
        async with client() as c:
            first = await c.post(
                "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
            )
            duplicate = await c.post(
                "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
            )
        await pending[0]
        pending[1].close()

        assert first.status_code == 200
        assert duplicate.json() == {"ok": True}
        [(delivered, state)] = channel.delivered
        assert delivered.type == channels.protocol.MESSAGE_COMPLETED
        assert delivered.data["message"] == "The coder fixed it."
        assert state == {"thread": "1"}
        record = await subagents.get(launch["id"])
        assert record is not None
        assert record["completion_delivered"] is True
        transcript = await events.read(chat.id, "messages")
        assert len(transcript) == 1
        reply = ai.messages.Message.model_validate(transcript[0][1])
        assert reply.role == "assistant"
        assert reply.text == "The coder fixed it."
        assert await events.read(chat.id, "ui") == [
            (
                0,
                {
                    "type": "task.changed",
                    "launch_id": launch["id"],
                    "state": "complete",
                },
            ),
            (1, {"type": "messages.changed"}),
        ]
        assert seen_wake is not None
        assert seen_wake.role == "user"
        assert launch["id"] in seen_wake.text
        assert "Call check_subagent" in seen_wake.text
        recorded = await activity.status(chat.id, launch["id"])
        assert recorded["events"] == [
            {"cursor": 0, "kind": "state_transition", "summary": "state changed to complete"}
        ]
        saved = await chats.get(chat.id)
        assert saved is not None
        assert saved.status == "done"
        assert saved.artifact == "https://github.com/a/b/pull/1"
    finally:
        if previous is None:
            server.bot.channels.pop("fake", None)
        else:
            server.bot.channels["fake"] = previous


async def test_devbox_attention_wakes_dispatcher_and_exposes_question(monkeypatch):
    seen = None

    async def fake_turn(chat_id, record, wake):
        nonlocal seen
        seen = (chat_id, wake)
        return server.CompletionOutcome(notify=False)

    pending = []
    monkeypatch.setattr(server, "_run_completion_turn", fake_turn)
    monkeypatch.setattr(server, "_spawn", pending.append)
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    await chats.finish(chat.id, "running")
    launch = await subagents.create(chat.id, "devbox_1", "inspect", "secret")
    launch["task_id"] = "task_1"
    await subagents.save(launch)
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {
            "taskId": "task_1",
            "state": "attention-required",
            "seq": 1,
            "result": {"question": "Which repository path should I use?"},
        },
    }

    async with client() as c:
        response = await c.post(
            "/channels/v1/devbox",
            params={"launch_id": launch["id"], "secret": "secret"},
            json=body,
        )
    await pending[0]

    assert response.status_code == 200
    assert seen is not None
    assert seen[0] == chat.id
    assert "attention-required" in seen[1].text
    status = await activity.status(chat.id, launch["id"])
    assert status["result"] == {"question": "Which repository path should I use?"}
    saved = await chats.get(chat.id)
    assert saved is not None and saved.status == "running"
    assert await events.read(chat.id, "ui") == [
        (0, {"type": "task.changed", "launch_id": launch["id"], "state": "attention-required"})
    ]


async def test_devbox_completion_schedules_retry_without_duplicate_transcript(monkeypatch):
    class FlakyChannel:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        async def on_event(self, event, state):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")

    async def fake_turn(chat_id, record, wake):
        await events.append(
            chat_id, "messages", ai.assistant_message("done").model_dump(mode="json")
        )
        return server.CompletionOutcome(notify=True, message="done")

    pending = []
    monkeypatch.setattr(server, "_run_completion_turn", fake_turn)
    monkeypatch.setattr(server, "_spawn", pending.append)
    channel = FlakyChannel()
    previous = server.bot.channels.get("flaky")
    server.bot.channels["flaky"] = channel
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("flaky:thread", "flaky", space.id, "task", {})
        launch = await subagents.create(chat.id, "devbox_1", "do it", "secret")
        launch["task_id"] = "task_1"
        await subagents.save(launch)
        body = {
            "kind": "taskStateChange",
            "taskStateChange": {
                "taskId": "task_1",
                "state": "complete",
                "seq": 1,
                "result": {"summary": "done"},
            },
        }
        async with client() as c:
            failed = await c.post(
                "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
            )
            retried = await c.post(
                "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
            )
        assert failed.status_code == 200
        assert retried.status_code == 200
        assert len(pending) == 2
        await pending[0]
        await pending[1]
        assert channel.calls == 2
        assert len(await events.read(chat.id, "messages")) == 1
    finally:
        if previous is None:
            server.bot.channels.pop("flaky", None)
        else:
            server.bot.channels["flaky"] = previous


async def test_completion_history_ends_with_unpersisted_user_wake(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    await events.append(
        chat.id, "messages", ai.assistant_message("Work started.").model_dump(mode="json")
    )
    wake = ai.user_message("Check coder status.")
    seen = None

    class FakeRun:
        output = server.CompletionOutcome(notify=False)
        messages = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeAgent:
        def run(self, model, history, output_type=None):
            nonlocal seen
            seen = history
            run = FakeRun()
            run.messages = list(history)
            return run

    monkeypatch.setattr(server.dispatcher, "agent_for", lambda record: FakeAgent())
    outcome = await server._run_completion_turn(chat.id, {"id": chat.id}, wake)
    assert outcome.notify is False
    assert seen is not None
    assert [message.role for message in seen[-2:]] == ["assistant", "user"]
    stored = await events.read(chat.id, "messages")
    assert len(stored) == 1
    assert ai.messages.Message.model_validate(stored[0][1]).text == "Work started."


async def test_spawn_keeps_background_task_alive():
    ran = asyncio.Event()

    async def work():
        ran.set()

    server._spawn(work())
    await asyncio.wait_for(ran.wait(), 1)


def test_spawn_uses_wait_until_on_vercel(monkeypatch):
    pending = []
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(server.vercel.functions, "wait_until", pending.append)

    async def work():
        pass

    coro = work()
    server._spawn(coro)

    assert pending == [coro]
    coro.close()


async def test_manual_devbox_create_returns_before_background_provision(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    spawned = []
    monkeypatch.setattr(server, "_spawn", spawned.append)

    async with client() as c:
        response = await c.post(
            f"/api/chats/{chat.id}/devboxes",
            json={
                "title": "manual",
                "repos": ["vercel/vercel-py"],
                "setup_script": "uv sync",
                "ports": [8000],
            },
        )

    assert response.status_code == 202
    assert response.json()["state"] == "creating"
    [record] = await server.devboxes.list_for_chat(chat.id)
    assert record["setup_script"] == "uv sync"
    assert record["ports"] == [8000]
    assert len(spawned) == 1
    spawned[0].close()


async def test_manual_devbox_suggestion_uses_chat_space(monkeypatch):
    space = await server.spaces.create("docs")
    space.about = "Build documentation."
    space.repos = ["acme/docs"]
    await server.spaces.save(space)
    chat = await chats.create(space.id, "task")
    seen = []

    async def suggest(selected):
        seen.append(selected)
        return server.sandbox.Launch(title="docs", repos=selected.repos)

    monkeypatch.setattr(server.sandbox, "suggest", suggest)
    async with client() as c:
        response = await c.get(f"/api/chats/{chat.id}/devboxes/suggestion")

    assert response.status_code == 200
    assert response.json()["repos"] == ["acme/docs"]
    assert seen == [space]


async def test_manual_terminal_create_requires_ready_owned_devbox():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", [])

    async with client() as c:
        not_ready = await c.post(
            f"/api/chats/{chat.id}/devboxes/{workspace['id']}/terminals"
        )
        unknown = await c.post(
            f"/api/chats/other/devboxes/{workspace['id']}/terminals"
        )

    assert not_ready.status_code == 409
    assert unknown.status_code == 404


async def test_manual_terminal_create_is_listed_on_devbox():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", [])
    workspace["state"] = "ready"
    workspace["box"] = {"id": "box_1", "url": "https://box.example"}
    await server.devboxes.save(workspace)

    async with client() as c:
        created = await c.post(
            f"/api/chats/{chat.id}/devboxes/{workspace['id']}/terminals"
        )
        listed = await c.get(f"/api/chats/{chat.id}/devboxes")

    assert created.status_code == 201
    assert created.json()["title"] == "bash 1"
    assert listed.json()[0]["terminals"] == [
        {key: value for key, value in created.json().items() if key != "chat_id"}
    ]


async def test_manual_terminal_delete_stops_session_and_removes_record(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", [])
    workspace["state"] = "ready"
    workspace["box"] = {"id": "box_1", "url": "https://box.example"}
    await server.devboxes.save(workspace)
    terminal = await terminals.create(chat.id, workspace["id"], "bash")
    terminal["session_id"] = "session_1"
    await terminals.save(terminal)
    sent = []

    async def send_tty_input(url, session_id, data, followup=None):
        sent.append((url, session_id, data, followup))

    monkeypatch.setattr(server.devbox, "send_tty_input", send_tty_input)

    async with client() as c:
        response = await c.delete(f"/api/chats/{chat.id}/terminals/{terminal['id']}")

    assert response.status_code == 204
    assert sent == [
        ("https://box.example", "session_1", b"\x03", b"exit\r"),
    ]
    assert await terminals.get(terminal["id"]) is None


async def test_subagent_delete_interrupts_running_task_and_removes_records(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", [])
    workspace["state"] = "ready"
    workspace["box"] = {"id": "box_1", "url": "https://box.example"}
    await server.devboxes.save(workspace)
    launch = await subagents.create(chat.id, workspace["id"], "work", "secret")
    launch.update(task_id="task_1", session_id="session_1", state="running")
    await subagents.save(launch)
    sent = []
    deleted = []

    async def send_tty_input(url, session_id, data):
        sent.append((url, session_id, data))

    async def delete_task(task_id):
        deleted.append(task_id)

    monkeypatch.setattr(server.devbox, "send_tty_input", send_tty_input)
    monkeypatch.setattr(server.devbox, "delete_task", delete_task)

    async with client() as c:
        response = await c.delete(f"/api/chats/{chat.id}/subagents/{launch['id']}")

    assert response.status_code == 204
    assert sent == [("https://box.example", "session_1", b"\x03")]
    assert deleted == ["task_1"]
    assert await subagents.get(launch["id"]) is None


async def test_devbox_delete_stops_box_and_cascades_local_records(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", [])
    workspace["state"] = "ready"
    workspace["box"] = {"id": "box_1", "url": "https://box.example"}
    await server.devboxes.save(workspace)
    terminal = await terminals.create(chat.id, workspace["id"], "bash")
    launch = await subagents.create(chat.id, workspace["id"], "work", "secret")
    deleted = []

    async def delete_box(box_id):
        deleted.append(box_id)

    monkeypatch.setattr(server.devbox, "delete_box", delete_box)

    async with client() as c:
        response = await c.delete(f"/api/chats/{chat.id}/devboxes/{workspace['id']}")

    assert response.status_code == 204
    assert deleted == ["box_1"]
    assert await server.devboxes.get(workspace["id"]) is None
    assert await terminals.get(terminal["id"]) is None
    assert await subagents.get(launch["id"]) is None


async def test_devbox_delete_rejects_creating_box():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", [])

    async with client() as c:
        response = await c.delete(f"/api/chats/{chat.id}/devboxes/{workspace['id']}")

    assert response.status_code == 409
    assert response.json() == {"detail": "devbox is still being created"}
    assert await server.devboxes.get(workspace["id"]) is not None


async def test_chat_devboxes_group_subagents_without_secrets():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    workspace = await server.devboxes.create(chat.id, "main", ["a/b"])
    workspace["state"] = "ready"
    await server.devboxes.save(workspace)
    launch = await subagents.create(chat.id, workspace["id"], "inspect the bug", "secret")
    launch["task_id"] = "task_1"
    launch["session_id"] = "session_1"
    await subagents.save(launch)

    async with client() as c:
        response = await c.get(f"/api/chats/{chat.id}/devboxes")
    assert response.status_code == 200
    [listed] = response.json()
    assert listed["id"] == workspace["id"]
    assert listed["repos"] == ["a/b"]
    assert listed["terminals"] == []
    assert listed["subagents"] == [
        {
            "id": launch["id"],
            "devbox_id": workspace["id"],
            "title": "inspect the bug",
            "task_id": "task_1",
            "session_id": "session_1",
            "state": "creating",
            "created_at": launch["created_at"],
        }
    ]
    assert "webhook_secret" not in listed["subagents"][0]


async def test_task_tty_accepts_before_reporting_unknown_subagent():
    class FakeWebSocket:
        def __init__(self):
            self.accepted = False
            self.closed = None

        async def accept(self):
            self.accepted = True

        async def close(self, code=1000, reason=None):
            assert self.accepted
            self.closed = (code, reason)

    ws = FakeWebSocket()
    await server.task_tty(ws, "chat_without_devbox", "subagent_missing")
    assert ws.closed == (4404, "unknown subagent")


async def test_task_tty_recovers_session_id_from_control_plane(monkeypatch):
    chat = await chats.create(None, "task")
    workspace = await server.devboxes.create(chat.id, "main", ["a/b"])
    workspace["state"] = "ready"
    workspace["box"] = {"id": "box_1", "url": "https://box.example"}
    await server.devboxes.save(workspace)
    launch = await subagents.create(chat.id, workspace["id"], "inspect", "secret")
    launch["task_id"] = "task_1"
    await subagents.save(launch)
    bridged = []

    async def get_task(task_id):
        assert task_id == "task_1"
        return {"task_id": task_id, "session_id": "session_1", "state": "running"}

    async def bridge(ws, found_workspace, session_id):
        bridged.append((found_workspace["id"], session_id))

    class FakeWebSocket:
        async def accept(self):
            pass

        async def close(self, code=1000, reason=None):
            raise AssertionError((code, reason))

    monkeypatch.setattr(server.devbox, "get_task", get_task)
    monkeypatch.setattr(server, "_bridge_tty", bridge)

    await server.task_tty(FakeWebSocket(), chat.id, launch["id"])

    assert bridged == [(workspace["id"], "session_1")]
    assert (await subagents.get(launch["id"]))["session_id"] == "session_1"


async def test_devbox_assistant_event_is_stored_once():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    launch = await subagents.create(chat.id, "devbox_1", "do it", "secret")
    launch["task_id"] = "task_1"
    await subagents.save(launch)
    body = {
        "kind": "assistantEvent",
        "assistantEvent": {
            "taskId": "task_1",
            "ts": "2026-08-20T12:00:00.123456789Z",
            "event": {"name": "assistant_message", "body": {"text": "Inspecting the webhook"}},
        },
    }
    async with client() as c:
        first = await c.post(
            "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
        )
        duplicate = await c.post(
            "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
        )
    assert first.status_code == 200
    assert duplicate.json() == {"ok": True, "duplicate": True}
    status = await activity.status(chat.id, launch["id"])
    assert status["events"] == [
        {"cursor": 0, "kind": "assistant_event", "summary": "Inspecting the webhook"}
    ]
    assert await events.read(chat.id, "messages") == []


async def test_devbox_completion_claims_task_id_from_early_callback():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    launch = await subagents.create(chat.id, "devbox_1", "do it", "secret")
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {"taskId": "task_early", "state": "running", "seq": 1},
    }
    async with client() as c:
        response = await c.post(
            "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
        )
    assert response.status_code == 200
    record = await subagents.get(launch["id"])
    assert record is not None
    assert record["task_id"] == "task_early"
    assert record["state"] == "running"


async def test_concurrent_task_callbacks_keep_separate_state(monkeypatch):
    monkeypatch.setattr(server.vercel.functions, "wait_until", lambda coro: coro.close())
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    first = await subagents.create(chat.id, "devbox_1", "first", "one")
    first["task_id"] = "task_1"
    await subagents.save(first)
    second = await subagents.create(chat.id, "devbox_1", "second", "two")
    second["task_id"] = "task_2"
    await subagents.save(second)

    async with client() as c:
        response = await c.post(
            "/channels/v1/devbox",
            params={"launch_id": first["id"], "secret": "one"},
            json={
                "kind": "taskStateChange",
                "taskStateChange": {"taskId": "task_1", "state": "complete", "seq": 2},
            },
        )
    assert response.status_code == 200
    saved_first = await subagents.get(first["id"])
    saved_second = await subagents.get(second["id"])
    assert saved_first is not None and saved_first["state"] == "complete"
    assert saved_second is not None and saved_second["state"] == "creating"


async def test_devbox_completion_rejects_wrong_secret():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    launch = await subagents.create(chat.id, "devbox_1", "do it", "right")
    launch["task_id"] = "task_1"
    await subagents.save(launch)
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {"taskId": "task_1", "state": "complete", "seq": 1},
    }
    async with client() as c:
        response = await c.post(
            "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "wrong"}, json=body
        )
    assert response.status_code == 401
