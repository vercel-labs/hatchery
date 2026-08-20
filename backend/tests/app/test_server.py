import asyncio

import httpx

import ai
import channels
from app import server
from store import chats, events


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

    async def fake_turn(chat_id, record):
        await events.append(
            chat_id, "messages", ai.assistant_message("The coder fixed it.").model_dump(mode="json")
        )
        return "The coder fixed it."

    pending = []
    monkeypatch.setattr(server, "_run_dispatcher_turn", fake_turn)
    monkeypatch.setattr(server.vercel.functions, "wait_until", pending.append)
    channel = FakeChannel()
    previous = server.bot.channels.get("fake")
    server.bot.channels["fake"] = channel
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("fake:thread", "fake", space.id, "task", {"thread": "1"})
        await events.append(
            chat.id,
            "worker",
            {
                "id": chat.id,
                "task_id": "task_1",
                "webhook_secret": "secret",
                "webhook_seq": 0,
                "completion_delivered": False,
            },
        )
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
                f"/channels/v1/devbox?chat_id={chat.id}&secret=secret", json=body
            )
            duplicate = await c.post(
                f"/channels/v1/devbox?chat_id={chat.id}&secret=secret", json=body
            )
        await pending[0]
        pending[1].close()

        assert first.status_code == 200
        assert duplicate.json() == {"ok": True}
        [(delivered, state)] = channel.delivered
        assert delivered.type == channels.protocol.MESSAGE_COMPLETED
        assert delivered.data["message"] == "The coder fixed it."
        assert state == {"thread": "1"}
        record = await events.tail(chat.id, "worker")
        assert record["completion_delivered"] is True
        transcript = await events.read(chat.id, "messages")
        completion = ai.messages.Message.model_validate(transcript[-2][1])
        reply = ai.messages.Message.model_validate(transcript[-1][1])
        assert completion.role == "user"
        assert completion.text == (
            '<coder_completion task_id="task_1" state="complete">\n'
            '{"summary":"fixed it","prs":[{"url":"https://github.com/a/b/pull/1"}]}\n'
            "</coder_completion>"
        )
        assert reply.role == "assistant"
        assert reply.text == "The coder fixed it."
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

    async def fake_turn(chat_id, record):
        await events.append(
            chat_id, "messages", ai.assistant_message("done").model_dump(mode="json")
        )
        return "done"

    pending = []
    monkeypatch.setattr(server, "_run_dispatcher_turn", fake_turn)
    monkeypatch.setattr(server.vercel.functions, "wait_until", pending.append)
    channel = FlakyChannel()
    previous = server.bot.channels.get("flaky")
    server.bot.channels["flaky"] = channel
    try:
        space = await server.spaces.default()
        chat, _ = await chats.claim("flaky:thread", "flaky", space.id, "task", {})
        await events.append(
            chat.id,
            "worker",
            {"id": chat.id, "task_id": "task_1", "webhook_secret": "secret"},
        )
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
                f"/channels/v1/devbox?chat_id={chat.id}&secret=secret", json=body
            )
            retried = await c.post(
                f"/channels/v1/devbox?chat_id={chat.id}&secret=secret", json=body
            )
        assert failed.status_code == 200
        assert retried.status_code == 200
        assert len(pending) == 2
        await pending[0]
        await pending[1]
        assert channel.calls == 2
        assert len(await events.read(chat.id, "messages")) == 2
    finally:
        if previous is None:
            server.bot.channels.pop("flaky", None)
        else:
            server.bot.channels["flaky"] = previous


async def test_spawn_keeps_background_task_alive():
    ran = asyncio.Event()

    async def work():
        ran.set()

    server._spawn(work())
    await asyncio.wait_for(ran.wait(), 1)


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


async def test_devbox_completion_claims_task_id_from_early_callback():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    await events.append(
        chat.id,
        "worker",
        {"id": chat.id, "webhook_secret": "secret", "webhook_seq": 0},
    )
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {"taskId": "task_early", "state": "running", "seq": 1},
    }
    async with client() as c:
        response = await c.post(
            f"/channels/v1/devbox?chat_id={chat.id}&secret=secret", json=body
        )
    assert response.status_code == 200
    record = await events.tail(chat.id, "worker")
    assert record["task_id"] == "task_early"
    assert record["task_state"] == "running"


async def test_devbox_completion_rejects_wrong_secret():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    await events.append(
        chat.id,
        "worker",
        {"id": chat.id, "task_id": "task_1", "webhook_secret": "right"},
    )
    body = {
        "kind": "taskStateChange",
        "taskStateChange": {"taskId": "task_1", "state": "complete", "seq": 1},
    }
    async with client() as c:
        response = await c.post(
            f"/channels/v1/devbox?chat_id={chat.id}&secret=wrong", json=body
        )
    assert response.status_code == 401
