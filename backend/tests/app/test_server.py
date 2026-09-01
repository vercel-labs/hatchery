import asyncio
from unittest import mock

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.frames import Close
from websockets.http11 import Response

import ai
import ai.experimental_telemetry
import channels
from app import server
from store import chats, events


def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def generated_topic(monkeypatch):
    async def generate(prompt):
        return "Test request"

    monkeypatch.setattr(server.topic, "generate", generate)


async def test_browser_api_requires_session(monkeypatch):
    async def current_user(_request):
        return None

    monkeypatch.setattr(server.auth, "current_user", current_user)
    async with client() as c:
        protected = await c.get("/api/spaces")
        health = await c.get("/api/health")
        identity = await c.get("/api/auth/me")

    assert protected.status_code == 401
    assert protected.json() == {"detail": "sign in required"}
    assert health.status_code == 200
    assert identity.status_code == 200
    assert identity.json() == {"user": None}


async def test_websocket_auth_rejects_missing_session(monkeypatch):
    async def session_user(_session_id):
        return None

    monkeypatch.setattr(server.auth, "session_user", session_user)
    ws = BridgeWebSocket()
    ws.cookies = {}
    ws.headers = {}

    assert await server._authenticate_websocket(ws) is False
    assert ws.closed == (4401, "sign in required")


async def test_legacy_chat_is_claimed_on_direct_access():
    chat = await chats.create(None, "legacy")

    async with client() as c:
        response = await c.get(f"/api/chats/{chat.id}/messages")

    assert response.status_code == 200
    assert (await chats.get(chat.id)).user_id == "user_test"


async def test_chat_routes_hide_another_users_chat(monkeypatch):
    chat = await chats.create(None, "private", user_id="user_other")

    async with client() as c:
        listed = await c.get("/api/chats")
        direct = await c.get(f"/api/chats/{chat.id}/messages")

    assert listed.json() == []
    assert direct.status_code == 404


async def test_github_connection_routes(monkeypatch):
    seen = {}

    async def begin(request, user):
        seen["authorized"] = user["id"]
        return server.fastapi.responses.RedirectResponse("https://connect.example")

    async def disconnect(user):
        seen["disconnected"] = user["id"]

    async def token(_user_id, _installation_id=None):
        return "token"

    monkeypatch.setattr(server.auth, "begin_github", begin)
    monkeypatch.setattr(server.auth, "disconnect_github", disconnect)
    monkeypatch.setattr(server.auth, "github_token", token)
    monkeypatch.setattr(
        server.auth,
        "github_connection",
        lambda user: {"login": "octocat", "id": "42"},
    )

    async with client() as c:
        status = await c.get("/api/connections/github")
        authorized = await c.get("/api/connections/github/authorize", follow_redirects=False)
        disconnected = await c.delete(
            "/api/connections/github", headers={"origin": "http://test"}
        )

    assert status.json() == {"connection": {"login": "octocat", "id": "42"}}
    assert authorized.headers["location"] == "https://connect.example"
    assert disconnected.status_code == 204
    assert seen == {"authorized": "user_test", "disconnected": "user_test"}


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

    flush = mock.Mock()
    monkeypatch.setattr(server.dispatcher, "agent_for", agent_for)
    monkeypatch.setattr(server.ai.ui.ai_sdk, "to_sse", fake_sse)
    monkeypatch.setattr(server.telemetry, "flush", flush)
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
    flush.assert_called_once_with()


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




















async def test_sandbox_routes_use_chat_scoped_control_plane(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    seen = {}

    class Record:
        id = "wrk_1"

        def model_dump(self, exclude=None):
            assert exclude == {"daemon_token"}
            return {"id": self.id, "chat_id": chat.id, "title": "sandbox"}

    async def list_all(chat_id):
        seen["listed"] = chat_id
        return [Record()]

    async def create(chat_id, launch):
        seen["created"] = (chat_id, launch)
        return Record()

    task = server.worker.Task(
        id="task_1", chat_id=chat.id, worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test", fx_session_id="fx_1",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    monkeypatch.setattr(server.sandbox, "list_all", list_all)
    monkeypatch.setattr(server.sandbox, "create", create)

    async with client() as c:
        listed = await c.get(f"/api/chats/{chat.id}/sandboxes")
        created = await c.post(
            f"/api/chats/{chat.id}/sandboxes",
            json={
                "title": "sandbox", "repos": [], "setup_script": None,
                "ports": [], "branch": None, "git_sha": None,
            },
        )
        old = await c.get(f"/api/chats/{chat.id}/devboxes")

    assert listed.status_code == 200
    assert created.status_code == 200
    assert listed.json()[0]["id"] == "wrk_1"
    assert listed.json()[0]["subagents"][0]["task_id"] == "task_1"
    assert listed.json()[0]["subagents"][0]["fx_session_id"] == "fx_1"
    assert created.json()["id"] == "wrk_1"
    assert seen["listed"] == chat.id
    assert seen["created"][0] == chat.id
    assert old.status_code == 404


async def test_worker_completion_wakes_dispatcher_with_hidden_persisted_result(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    task = server.worker.Task(
        id="task_1",
        chat_id=chat.id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        status="complete",
        event_sequence=3,
        result={"summary": "fixed and tested"},
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    delivered = []
    turns = []

    async def turn(chat_id, record, wake=None):
        turns.append([message for message in await server._transcript(chat_id)])
        await events.append(
            chat_id,
            "messages",
            ai.assistant_message("The subagent fixed and tested it.").model_dump(mode="json"),
        )
        return "The subagent fixed and tested it."

    async def deliver(chat_id, message):
        delivered.append((chat_id, message))
        return []

    monkeypatch.setattr(server, "_run_dispatcher_turn", turn)
    monkeypatch.setattr(server, "_deliver", deliver)
    await server.complete_worker_task(task)
    await server.complete_worker_task(task)

    stored = [ai.messages.Message.model_validate(data) for _, data in await events.read(chat.id, "messages")]
    assert [(message.role, message.text) for message in stored] == [
        (
            "user",
            '<subagent_result>\n{"subagent_id":"task_1","status":"complete","result":{"summary":"fixed and tested"}}\n</subagent_result>',
        ),
        ("assistant", "The subagent fixed and tested it."),
    ]
    assert stored[0].provider_metadata == {
        "hatchery": {"kind": "subagent_result", "subagent_id": "task_1"}
    }
    assert len(turns) == 1
    assert turns[0][-1].id == "subagent_result_task_1_3"
    assert delivered == [(chat.id, "The subagent fixed and tested it.")]
    assert (await server.worker.get_task(chat.id, task.id)).completion_delivered is True
    assert (await chats.get(chat.id)).status == "done"

    async with client() as c:
        visible = (await c.get(f"/api/chats/{chat.id}/messages")).json()
    assert [message["role"] for message in visible] == ["assistant"]


async def test_worker_completion_retries_delivery_without_rerunning_dispatcher(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    task = server.worker.Task(
        id="task_1", chat_id=chat.id, worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test", status="complete", event_sequence=2,
        result={"summary": "done"}, created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    turn_calls = 0
    delivery_calls = 0

    async def turn(chat_id, record, wake=None):
        nonlocal turn_calls
        turn_calls += 1
        await events.append(
            chat_id, "messages", ai.assistant_message("done").model_dump(mode="json")
        )
        return "done"

    async def deliver(chat_id, message):
        nonlocal delivery_calls
        delivery_calls += 1
        return ["temporary"] if delivery_calls == 1 else []

    monkeypatch.setattr(server, "_run_dispatcher_turn", turn)
    monkeypatch.setattr(server, "_deliver", deliver)
    await server.complete_worker_task(task)
    await server.complete_worker_task(task)

    stored = await events.read(chat.id, "messages")
    assert len(stored) == 2
    assert turn_calls == 1
    assert delivery_calls == 2
    assert (await server.worker.get_task(chat.id, task.id)).completion_delivered is True


async def test_task_readiness_reports_queue_state(monkeypatch):
    task = server.worker.Task(
        id="task_1", chat_id="chat_1", worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    record = type("Worker", (), {"id": "wrk_1"})()

    async def get_task(chat_id, task_id):
        return task

    async def get_worker(worker_id):
        return record

    async def daemon_health(found):
        assert found is record
        return {
            "ok": True,
            "version": 6,
            "queue_connected": False,
            "queue_error": "HTTP 502: tunnel offline",
        }

    async def tty_sessions(found):
        assert found is record
        return []

    monkeypatch.setattr(server.worker, "get_task", get_task)
    monkeypatch.setattr(server.worker, "get", get_worker)
    monkeypatch.setattr(server.worker.sandbox, "daemon_health", daemon_health)
    monkeypatch.setattr(server.worker.sandbox, "tty_sessions", tty_sessions)

    readiness = await server.task_readiness("chat_1", "task_1")

    assert readiness == {
        "state": "pending",
        "session_ready": False,
        "daemon": {
            "ok": True,
            "version": 6,
            "queue_connected": False,
            "queue_error": "HTTP 502: tunnel offline",
        },
    }


async def test_task_readiness_requires_actual_daemon_session(monkeypatch):
    task = server.worker.Task(
        id="task_1", chat_id="chat_1", worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test", status="running",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    record = type("Worker", (), {"id": "wrk_1"})()

    async def get_task(chat_id, task_id):
        return task

    async def get_worker(worker_id):
        return record

    async def daemon_health(found):
        return {"ok": True, "queue_connected": True, "queue_error": None}

    async def tty_sessions(found):
        return [{"id": "task_1", "running": True}]

    monkeypatch.setattr(server.worker, "get_task", get_task)
    monkeypatch.setattr(server.worker, "get", get_worker)
    monkeypatch.setattr(server.worker.sandbox, "daemon_health", daemon_health)
    monkeypatch.setattr(server.worker.sandbox, "tty_sessions", tty_sessions)

    readiness = await server.task_readiness("chat_1", "task_1")

    assert readiness["session_ready"] is True


async def test_worker_event_continues_and_closes_agent_run(monkeypatch):
    seen = []

    @ai.experimental_telemetry.adapter
    async def capture(span):
        yield
        seen.append(span)

    ai.experimental_telemetry.register(capture)
    parent = ai.experimental_telemetry.create_span("hatchery.agent_run").stamp_start()
    task = server.worker.Task(
        id="task_trace",
        chat_id="chat_trace",
        worker_id="wrk_trace",
        title="trace",
        prompt="trace it",
        model="openai/test",
        telemetry_span=parent.model_dump(mode="json"),
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)

    async def complete(task):
        pass

    monkeypatch.setattr(server, "complete_worker_task", complete)
    try:
        await server.worker_event(
            server.worker_protocol.Event(
                id="evt_transcript",
                worker_id=task.worker_id,
                task_id=task.id,
                sequence=0,
                type="task.transcript",
                created_at="2026-08-31T00:00:01+00:00",
                payload={
                    "kind": "tool.call",
                    "tool_call_id": "call_trace",
                    "tool_name": "read_file",
                    "arguments": '{"path":"README.md"}',
                    "session_id": "session_trace",
                    "truncated": False,
                },
            )
        )
        await server.worker_event(
            server.worker_protocol.Event(
                id="evt_result",
                worker_id=task.worker_id,
                task_id=task.id,
                sequence=1,
                type="task.transcript",
                created_at="2026-08-31T00:00:02+00:00",
                payload={
                    "kind": "tool.result",
                    "tool_call_id": "call_trace",
                    "output": "README contents",
                    "error": False,
                    "truncated": False,
                },
            )
        )
        await server.worker_event(
            server.worker_protocol.Event(
                id="evt_output",
                worker_id=task.worker_id,
                task_id=task.id,
                sequence=2,
                type="task.output",
                created_at="2026-08-31T00:00:03+00:00",
                payload={"text": "Finished reading."},
            )
        )
        await server.worker_event(
            server.worker_protocol.Event(
                id="evt_trace",
                worker_id=task.worker_id,
                task_id=task.id,
                sequence=3,
                type="task.completed",
                created_at="2026-08-31T00:00:04+00:00",
                payload={"summary": "done"},
            )
        )
    finally:
        ai.experimental_telemetry.unregister(capture)

    transcript = next(span for span in seen if span.name == "fx.tool.call")
    result = next(span for span in seen if span.name == "fx.tool.result")
    assistant = next(span for span in seen if span.name == "fx.assistant")
    completed = next(span for span in seen if span.name == "fx.task.completed")
    assert transcript.trace_id == parent.trace_id
    assert transcript.parent_id == parent.id
    assert transcript.data.attrs["braintrust.input_json"] == '{"path": "README.md"}'
    assert transcript.data.attrs["braintrust.span_attributes"] == '{"type": "tool"}'
    assert transcript.data.attrs["gen_ai.operation.name"] == "execute_tool"
    assert transcript.data.attrs["gen_ai.tool.name"] == "read_file"
    assert transcript.data.attrs["gen_ai.tool.call.id"] == "call_trace"
    assert transcript.data.attrs["gen_ai.tool.call.arguments"] == '{"path":"README.md"}'
    assert result.data.attrs["braintrust.output_json"] == '"README contents"'
    assert result.data.attrs["gen_ai.tool.call.result"] == '"README contents"'
    assert result.data.attrs["tool_error"] is False
    assert assistant.data.attrs["braintrust.output_json"] == '{"text": "Finished reading."}'
    assert completed.trace_id == parent.trace_id
    assert completed.parent_id == parent.id
    stored = await server.worker.store.get_task(task.id)
    assert stored is not None
    assert stored.telemetry_span["ended_at"] is not None
    assert stored.telemetry_span["data"]["attrs"]["braintrust.output_json"] == '{"summary": "done"}'
    assert stored.telemetry_span["data"]["attrs"]["fx.session_id"] == "session_trace"
    assert stored.telemetry_span["data"]["attrs"]["fx.tool_call_count"] == 1
    assert stored.telemetry_span["events"][0]["name"] == "fx.tool.call"
    assert stored.telemetry_span["events"][0]["attrs"]["tool_name"] == "read_file"


async def test_worker_event_pushes_late_transcript_without_extending_run(monkeypatch):
    seen = []

    @ai.experimental_telemetry.adapter
    async def capture(span):
        yield
        seen.append(span)

    ai.experimental_telemetry.register(capture)
    parent = ai.experimental_telemetry.create_span("hatchery.agent_run").stamp_start()
    parent.stamp_end()
    ended_at = parent.ended_at
    task = server.worker.Task(
        id="task_late",
        chat_id="chat_late",
        worker_id="wrk_late",
        title="late",
        prompt="trace it",
        model="openai/test",
        status="complete",
        event_sequence=3,
        event_ids=["evt_completed"],
        telemetry_span=parent.model_dump(mode="json"),
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:03+00:00",
    )
    await server.worker.store.save_task(task)
    try:
        await server.worker_event(
            server.worker_protocol.Event(
                id="evt_late",
                worker_id=task.worker_id,
                task_id=task.id,
                sequence=1,
                type="task.transcript",
                created_at="2026-08-31T00:00:01+00:00",
                payload={"kind": "tool.result", "output": "done", "truncated": False},
            )
        )
    finally:
        ai.experimental_telemetry.unregister(capture)

    run = next(span for span in seen if span.name == "hatchery.agent_run")
    assert run.ended_at == ended_at
    assert run.events[-1].name == "fx.tool.result"
    stored = await server.worker.store.get_task(task.id)
    assert stored is not None
    assert stored.status == "complete"
    assert stored.transcript_event_count == 1


def test_worker_event_subscriber_is_serialized():
    subscription = next(
        item
        for item in server.vercel.queue.get_subscriptions()
        if item.func is server.worker_event
    )

    assert subscription.topic == server.worker_protocol.EVENT_TOPIC
    assert subscription.consumer_group == "hatchery-control-plane-v1"
    assert subscription.max_concurrency == 1


async def test_task_tty_rejects_pending_subagent(monkeypatch):
    task = server.worker.Task(
        id="task_1", chat_id="chat_1", worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )

    async def get_task(chat_id, task_id):
        return task

    monkeypatch.setattr(server.worker, "get_task", get_task)
    ws = FakeWebSocket()

    await server.task_tty(ws, "chat_1", "task_1")

    assert ws.closed == (4409, "subagent is waiting for the sandbox queue")


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        assert self.accepted
        self.closed = (code, reason)


async def test_task_tty_rejects_unknown_subagent():
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
    await server.task_tty(ws, "chat_1", "subagent_1")
    assert ws.closed == (4404, "unknown subagent")


class BridgeWebSocket:
    query_params = {}

    def __init__(self):
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        assert self.accepted
        self.closed = (code, reason)

    async def receive(self):
        await asyncio.Future()


async def test_tty_bridge_propagates_upstream_close(monkeypatch):
    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise websockets.ConnectionClosedError(
                Close(4404, "session not found"), None
            )

    monkeypatch.setattr(server.worker.sandbox, "tty", lambda record: ("wss://tty.example", {}))
    monkeypatch.setattr(server.websockets.asyncio.client, "connect", lambda *args, **kwargs: Connection())
    ws = BridgeWebSocket()

    await server._bridge_tty(ws, type("Worker", (), {"id": "wrk_1"})(), "task_1")

    assert ws.closed == (4404, "session not found")


async def test_tty_bridge_maps_auth_rejection(monkeypatch):
    class Connection:
        async def __aenter__(self):
            raise websockets.InvalidStatus(Response(401, "Unauthorized", Headers()))

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(server.worker.sandbox, "tty", lambda record: ("wss://tty.example", {}))
    monkeypatch.setattr(server.websockets.asyncio.client, "connect", lambda *args, **kwargs: Connection())
    ws = BridgeWebSocket()

    await server._bridge_tty(ws, type("Worker", (), {"id": "wrk_1"})(), "task_1")

    assert ws.closed == (4401, "upstream rejected connection (401)")


async def test_tty_bridge_maps_connection_failure(monkeypatch):
    class Connection:
        async def __aenter__(self):
            raise OSError("unreachable")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(server.worker.sandbox, "tty", lambda record: ("wss://tty.example", {}))
    monkeypatch.setattr(server.websockets.asyncio.client, "connect", lambda *args, **kwargs: Connection())
    ws = BridgeWebSocket()

    await server._bridge_tty(ws, type("Worker", (), {"id": "wrk_1"})(), "task_1")

    assert ws.closed == (1011, "upstream connection failed")


async def test_dispatcher_turn_flushes_telemetry(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "trace me")
    await events.append(
        chat.id, "messages", ai.user_message("hello").model_dump(mode="json")
    )

    class FakeRun:
        def __init__(self, history):
            self.messages = [*history, ai.assistant_message("done")]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeAgent:
        def run(self, model, history):
            return FakeRun(history)

    flush = mock.Mock()
    monkeypatch.setattr(server.dispatcher, "agent_for", lambda record: FakeAgent())
    monkeypatch.setattr(server.telemetry, "flush", flush)

    assert await server._run_dispatcher_turn(chat.id, {"id": chat.id}) == "done"
    flush.assert_called_once_with()
