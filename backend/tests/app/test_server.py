import asyncio

import httpx

import ai
import channels
from app import server
from store import activity, chats, events, tasks


def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_spaces_seed_default():
    async with client() as c:
        listed = (await c.get("/api/spaces")).json()
    assert [s["id"] for s in listed] == ["spc_fabricator"]


async def test_chat_create_and_list():
    async with client() as c:
        created = (await c.post("/api/chats", json={})).json()
        assert created["space_id"] == "spc_fabricator"
        assert created["title"] == "new chat"
        listed = (await c.get("/api/chats")).json()
    assert [x["id"] for x in listed] == [created["id"]]


async def test_chat_messages_from_store():
    for message in (ai.user_message("hi"), ai.assistant_message("hello")):
        await events.append("chat_x", "messages", message.model_dump(mode="json"))
    async with client() as c:
        ui = (await c.get("/api/chats/chat_x/messages")).json()
        empty = (await c.get("/api/chats/chat_empty/messages")).json()
    assert [m["role"] for m in ui] == ["user", "assistant"]
    assert ui[0]["parts"][0]["text"] == "hi"
    assert empty == []


def test_dedupe_tool_history_repairs_old_ui_duplicates():
    call = ai.messages.ToolCallPart(
        tool_call_id="call_1", tool_name="launch_coder", tool_args='{"task":"x"}'
    )
    result = ai.messages.ToolResultPart(
        tool_call_id="call_1", tool_name="launch_coder", result="accepted"
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


async def test_hub_lands_inbound_in_one_chat():
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


async def test_hub_dedupe_is_durable():
    hub = server.bot.hub
    assert await hub.dedupe("slack:ev1") is True
    assert await hub.dedupe("slack:ev1") is False


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
        return server.SupervisionOutcome(notify=True, message="The coder fixed it.")

    pending = []
    monkeypatch.setattr(server, "_run_supervision_turn", fake_turn)
    monkeypatch.setattr(server.vercel.functions, "wait_until", pending.append)
    channel = FakeChannel()
    previous = server.bot.channels.get("fake")
    server.bot.channels["fake"] = channel
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("fake:thread", "fake", space.id, "task", {"thread": "1"})
        launch = await tasks.create(chat.id, "fix it", "secret")
        launch["task_id"] = "task_1"
        await tasks.save(launch)
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
        record = await tasks.get(launch["id"])
        assert record is not None
        assert record["completion_delivered"] is True
        transcript = await events.read(chat.id, "messages")
        assert len(transcript) == 1
        reply = ai.messages.Message.model_validate(transcript[0][1])
        assert reply.role == "assistant"
        assert reply.text == "The coder fixed it."
        assert seen_wake is not None
        assert seen_wake.role == "user"
        assert launch["id"] in seen_wake.text
        assert "Call check_coder" in seen_wake.text
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
        return server.SupervisionOutcome(notify=True, message="done")

    pending = []
    monkeypatch.setattr(server, "_run_supervision_turn", fake_turn)
    monkeypatch.setattr(server.vercel.functions, "wait_until", pending.append)
    channel = FlakyChannel()
    previous = server.bot.channels.get("flaky")
    server.bot.channels["flaky"] = channel
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("flaky:thread", "flaky", space.id, "task", {})
        launch = await tasks.create(chat.id, "do it", "secret")
        launch["task_id"] = "task_1"
        await tasks.save(launch)
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


async def test_supervision_history_ends_with_unpersisted_user_wake(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    await events.append(
        chat.id, "messages", ai.assistant_message("Work started.").model_dump(mode="json")
    )
    wake = ai.user_message("Check coder status.")
    seen = None

    class FakeRun:
        output = server.SupervisionOutcome(notify=False)
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
    outcome = await server._run_supervision_turn(chat.id, {"id": chat.id}, wake)
    assert outcome.notify is False
    assert seen is not None
    assert [message.role for message in seen[-2:]] == ["assistant", "user"]
    stored = await events.read(chat.id, "messages")
    assert len(stored) == 1
    assert ai.messages.Message.model_validate(stored[0][1]).text == "Work started."


async def test_periodic_supervision_can_stay_silent(monkeypatch):
    space = await server.spaces.default()
    chat, _ = await chats.claim("fake:quiet", "fake", space.id, "task", {})
    launch = await tasks.create(chat.id, "do it", "secret")
    launch["task_id"] = "task_1"
    launch["state"] = "running"
    await tasks.save(launch)
    await activity.append(
        launch["id"],
        "assistant_event",
        {"name": "assistant_message", "body": {"text": "Still inspecting"}},
    )

    seen = []

    async def quiet_turn(chat_id, record, wake):
        seen.append(wake.text)
        return server.SupervisionOutcome(notify=False)

    monkeypatch.setattr(server, "_run_supervision_turn", quiet_turn)
    assert await server.supervise_task(launch["id"], "periodic") is False
    saved = await tasks.get(launch["id"])
    assert saved is not None
    assert saved["supervision_cursor"] == 0
    assert saved["completion_delivered"] is False
    assert await events.read(chat.id, "messages") == []
    assert "after=-1" in seen[0]


async def test_spawn_keeps_background_task_alive():
    ran = asyncio.Event()

    async def work():
        ran.set()

    server._spawn(work())
    await asyncio.wait_for(ran.wait(), 1)


async def test_chat_tasks_lists_launches_without_secrets():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    launch = await tasks.create(chat.id, "inspect the bug", "secret")
    launch["task_id"] = "task_1"
    launch["session_id"] = "session_1"
    await tasks.save(launch)

    async with client() as c:
        response = await c.get(f"/api/chats/{chat.id}/tasks")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": launch["id"],
            "title": "inspect the bug",
            "task_id": "task_1",
            "session_id": "session_1",
            "state": "creating",
            "created_at": launch["created_at"],
        }
    ]


async def test_tty_accepts_before_reporting_session_not_ready():
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
    await server.tty(ws, "chat_without_worker")
    assert ws.closed == (4404, "no coder session for this chat")


async def test_devbox_assistant_event_is_stored_once():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    launch = await tasks.create(chat.id, "do it", "secret")
    launch["task_id"] = "task_1"
    await tasks.save(launch)
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
    launch = await tasks.create(chat.id, "do it", "secret")
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {"taskId": "task_early", "state": "running", "seq": 1},
    }
    async with client() as c:
        response = await c.post(
            "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "secret"}, json=body
        )
    assert response.status_code == 200
    record = await tasks.get(launch["id"])
    assert record is not None
    assert record["task_id"] == "task_early"
    assert record["state"] == "running"


async def test_concurrent_task_callbacks_keep_separate_state(monkeypatch):
    monkeypatch.setattr(server.vercel.functions, "wait_until", lambda coro: coro.close())
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    first = await tasks.create(chat.id, "first", "one")
    first["task_id"] = "task_1"
    await tasks.save(first)
    second = await tasks.create(chat.id, "second", "two")
    second["task_id"] = "task_2"
    await tasks.save(second)

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
    saved_first = await tasks.get(first["id"])
    saved_second = await tasks.get(second["id"])
    assert saved_first is not None and saved_first["state"] == "complete"
    assert saved_second is not None and saved_second["state"] == "creating"


async def test_devbox_completion_rejects_wrong_secret():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    launch = await tasks.create(chat.id, "do it", "right")
    launch["task_id"] = "task_1"
    await tasks.save(launch)
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {"taskId": "task_1", "state": "complete", "seq": 1},
    }
    async with client() as c:
        response = await c.post(
            "/channels/v1/devbox", params={"launch_id": launch["id"], "secret": "wrong"}, json=body
        )
    assert response.status_code == 401
