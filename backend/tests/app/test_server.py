import asyncio
import datetime
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

    async def slack_user(_team_id, _slack_user_id):
        return "user_test"

    async def github_user(_github_user_id):
        return "user_test"

    async def get_user(_user_id):
        return {"id": "user_test", "email": "test@vercel.com"}

    monkeypatch.setattr(server.topic, "generate", generate)
    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server.connections.auth_store, "github_user", github_user)
    monkeypatch.setattr(server.connections.auth_store, "get_user", get_user)


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


async def test_legacy_chat_is_claimed_on_direct_access(monkeypatch):
    chat = await chats.create(None, "legacy")

    async def current_user(_request):
        return {"id": "user_test", "email": "test@vercel.com"}

    monkeypatch.setattr(server.auth, "current_user", current_user)
    async with client() as c:
        response = await c.get(f"/api/chats/{chat.id}/messages")

    assert response.status_code == 200
    claimed = await chats.get(chat.id)
    assert claimed is not None and claimed.user_id == "user_test"
    assert claimed.author_display_name == "test@vercel.com"


async def test_browser_does_not_claim_unowned_channel_chat():
    chat, _ = await chats.claim("slack:C1:1.0", "slack", None, "legacy slack", {})

    async with client() as c:
        listed = await c.get("/api/chats")
        direct = await c.get(f"/api/chats/{chat.id}/messages")

    assert listed.json() == []
    assert direct.status_code == 404
    assert (await chats.get(chat.id)).user_id is None


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

    monkeypatch.setattr(server.connections, "begin_github", begin)
    monkeypatch.setattr(server.connections, "disconnect_github", disconnect)
    monkeypatch.setattr(server.connections, "github_token", token)
    monkeypatch.setattr(
        server.connections,
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


async def test_slack_connection_routes(monkeypatch):
    seen = {}

    async def begin(request, user):
        seen["authorized"] = user["id"]
        return server.fastapi.responses.RedirectResponse("https://connect.example")

    async def disconnect(user):
        seen["disconnected"] = user["id"]

    async def token(user_id):
        assert user_id == "user_test"
        return "token"

    monkeypatch.setattr(server.connections, "begin_slack", begin)
    monkeypatch.setattr(server.connections, "disconnect_slack", disconnect)
    monkeypatch.setattr(server.connections, "slack_token", token)
    monkeypatch.setattr(
        server.connections,
        "slack_connection",
        lambda user: {
            "team_id": "T1",
            "team": "Acme",
            "user_id": "U1",
            "user": "ada",
        },
    )

    async with client() as c:
        status = await c.get("/api/connections/slack")
        authorized = await c.get("/api/connections/slack/authorize", follow_redirects=False)
        disconnected = await c.delete(
            "/api/connections/slack", headers={"origin": "http://test"}
        )

    assert status.json() == {
        "connection": {
            "team_id": "T1",
            "team": "Acme",
            "user_id": "U1",
            "user": "ada",
        }
    }
    assert authorized.headers["location"] == "https://connect.example"
    assert disconnected.status_code == 204
    assert seen == {"authorized": "user_test", "disconnected": "user_test"}


async def test_vercel_cli_connection_routes(monkeypatch):
    seen = {}

    async def connection(user_id):
        assert user_id == "user_test"
        return {"username": "ada"}

    async def connect(user_id, token):
        seen["connected"] = (user_id, token)
        return {"username": "ada"}

    async def disconnect(user_id):
        seen["disconnected"] = user_id

    monkeypatch.setattr(server.connections, "vercel_cli_connection", connection)
    monkeypatch.setattr(server.connections, "connect_vercel_cli", connect)
    monkeypatch.setattr(server.connections, "disconnect_vercel_cli", disconnect)

    async with client() as c:
        status = await c.get("/api/connections/vercel-cli")
        connected = await c.put(
            "/api/connections/vercel-cli",
            json={"token": "private"},
            headers={"origin": "http://test"},
        )
        disconnected = await c.delete(
            "/api/connections/vercel-cli", headers={"origin": "http://test"}
        )

    assert status.json() == {"connection": {"username": "ada"}}
    assert connected.json() == {"connection": {"username": "ada"}}
    assert disconnected.status_code == 204
    assert seen == {
        "connected": ("user_test", "private"),
        "disconnected": "user_test",
    }


async def test_spaces_seed_default():
    async with client() as c:
        listed = (await c.get("/api/spaces")).json()
    assert [s["id"] for s in listed] == ["spc_hatchery"]
    assert "goal" not in listed[0]
    assert not listed[0]["about"].startswith("# hatchery")


async def test_space_warnings_check_main_repo_and_log(monkeypatch, caplog):
    space = await server.spaces.create("docs")
    space.repos = ["acme/main", "acme/secondary"]
    await server.spaces.save(space)
    checked = []

    async def warning(user_id, repo):
        checked.append((user_id, repo))
        return "Install the Hatchery GitHub app on acme."

    monkeypatch.setattr(server.connections, "github_repo_warning", warning)

    with caplog.at_level("WARNING", logger="app"):
        async with client() as c:
            response = await c.get("/api/spaces/warnings")

    assert response.json() == [
        {
            "space_id": space.id,
            "repo": "acme/main",
            "warning": "Install the Hatchery GitHub app on acme.",
        }
    ]
    assert checked == [("user_test", "acme/main")]
    assert "space main repository lacks Hatchery GitHub access" in caplog.text


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


async def test_space_delete_cascades_owner_scoped_jobs():
    space = await server.spaces.create("scheduled")
    own = await server.jobs.create(space.id, "user_test", "0 9 * * *", "Mine")
    other = await server.jobs.create(space.id, "user_other", "0 10 * * *", "Theirs")

    async with client() as c:
        response = await c.delete(f"/api/spaces/{space.id}")

    assert response.status_code == 204
    assert await server.jobs.get(own.id) is None
    assert await server.jobs.get(other.id) is None


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
                "repos": ["acme/app"],
                "resources": [
                    {"title": "docs", "url": "https://example.com/docs", "kind": "link"}
                ],
            },
        )
        listed = (await c.get("/api/spaces")).json()

    assert response.status_code == 200
    assert response.json()["repos"] == ["acme/app"]
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
            json={"repos": ["https://github.com/acme/app"], "resources": []},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422


async def test_job_routes_are_owner_scoped():
    await server.spaces.default()
    async with client() as c:
        created = await c.post(
            "/api/spaces/spc_hatchery/jobs",
            json={"schedule": "0 9 * * 1-5", "prompt": "Check reports"},
            headers={"origin": "http://test"},
        )
        listed = await c.get("/api/spaces/spc_hatchery/jobs")
        paused = await c.patch(
            f"/api/jobs/{created.json()['id']}/pause",
            json={"paused": True},
            headers={"origin": "http://test"},
        )
        invalid = await c.put(
            f"/api/jobs/{created.json()['id']}",
            json={"schedule": "0 0 9 * * *", "prompt": "bad"},
            headers={"origin": "http://test"},
        )
        deleted = await c.delete(
            f"/api/jobs/{created.json()['id']}", headers={"origin": "http://test"}
        )

    assert created.status_code == 200
    assert listed.json() == [created.json()]
    assert paused.json()["paused"] is True
    assert invalid.status_code == 422
    assert deleted.status_code == 204


async def test_cron_heartbeat_auth_and_reconciliation(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-test-secret")
    space = await server.spaces.default()
    job = await server.jobs.create(space.id, "user_test", "* * * * *", "Do work")
    job.next_run_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=2)).isoformat()
    server.jobs._write_job(job)
    starts = []

    async def start_turn(chat_id, origin, task_id=None, turn_id=None):
        starts.append((chat_id, origin, turn_id))
        await server.jobs.claim_run(turn_id, "run_1")
        return server.turns.ActiveTurn(turn_id, "run_1", origin, task_id, 0)

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    async with client() as c:
        denied = await c.get("/api/cron")
        first = await c.get(
            "/api/cron", headers={"authorization": "Bearer cron-test-secret"}
        )
        duplicate = await c.get(
            "/api/cron", headers={"authorization": "Bearer cron-test-secret"}
        )
        visible = await c.get("/api/chats")

    assert denied.status_code == 401
    assert first.json() == {"ok": True, "started": 1}
    assert duplicate.json() == {"ok": True, "started": 0}
    assert len(starts) == 1
    assert starts[0][1] == "cron"
    assert visible.json()[0]["trigger"] == f"cron:{job.id}"
    transcript = await server._transcript(starts[0][0])
    assert [message.text for message in transcript] == ["Do work"]


async def test_paused_pending_job_does_not_start(monkeypatch):
    secret = "cron-test-secret"
    monkeypatch.setenv("CRON_SECRET", secret)
    space = await server.spaces.default()
    job = await server.jobs.create(space.id, "user_test", "* * * * *", "Do work")
    now = datetime.datetime.now(datetime.UTC)
    job.next_run_at = (now - datetime.timedelta(minutes=1)).isoformat()
    server.jobs._write_job(job)
    await server.jobs.claim_due(now)
    await server.jobs.set_paused(job.id, True)
    starts = []

    async def start_turn(*args, **kwargs):
        starts.append((args, kwargs))

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    async with client() as c:
        response = await c.get(
            "/api/cron", headers={"authorization": f"Bearer {secret}"}
        )

    assert response.json() == {"ok": True, "started": 0}
    assert starts == []


async def test_chat_create_and_list(monkeypatch):
    async def current_user(_request):
        return {
            "id": "user_test",
            "name": "Ada Lovelace",
            "username": "ada",
            "email": "test@vercel.com",
        }

    monkeypatch.setattr(server.auth, "current_user", current_user)
    async with client() as c:
        created = (await c.post("/api/chats", json={})).json()
        assert created["space_id"] is None
        assert created["title"] == "new chat"
        assert created["author_display_name"] == "Ada Lovelace"
        listed = (await c.get("/api/chats")).json()
    assert [x["id"] for x in listed] == [created["id"]]
    assert listed[0]["author_display_name"] == "Ada Lovelace"


async def test_chat_archive_and_unarchive():
    chat = await chats.create(None, "work", user_id="user_test")

    async with client() as c:
        archived = await c.patch(
            f"/api/chats/{chat.id}/archive",
            json={"archived": True},
            headers={"origin": "http://test"},
        )
        listed = (await c.get("/api/chats")).json()
        unarchived = await c.patch(
            f"/api/chats/{chat.id}/archive",
            json={"archived": False},
            headers={"origin": "http://test"},
        )

    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert listed[0]["archived_at"] == archived.json()["archived_at"]
    assert unarchived.status_code == 200
    assert unarchived.json()["archived_at"] is None


async def test_chat_archive_rejects_active_turn(monkeypatch):
    chat = await chats.create(None, "running", user_id="user_test")

    async def active_turn(_chat_id):
        return object()

    monkeypatch.setattr(server.durable, "active_turn", active_turn)
    async with client() as c:
        response = await c.patch(
            f"/api/chats/{chat.id}/archive",
            json={"archived": True},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "chat has an active turn"}
    assert (await chats.get(chat.id)).archived_at is None


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
        user_id="user_test",
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


async def test_archived_chat_rejects_ui_post_before_persisting(monkeypatch):
    chat = await chats.create(None, "archived", user_id="user_test")
    await chats.set_archived(chat.id, True)
    started = []

    async def start_turn(*args):
        started.append(args)

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    message = ai.user_message("should not be stored")
    ui_message = ai.ui.ai_sdk.to_ui_messages([message])[0]
    async with client() as c:
        response = await c.post(
            "/api/chat",
            json={"chat_id": chat.id, "messages": [ui_message.model_dump(mode="json")]},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "chat is archived; unarchive it before posting"
    }
    assert await events.read(chat.id, "messages") == []
    assert started == []


async def test_archived_chat_rejects_inbound_with_explanation(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    delivered = []
    started = []

    async def deliver(chat_id, message, *, final=True):
        delivered.append((chat_id, message, final))
        return []

    async def start_turn(*args):
        started.append(args)

    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_deliver", deliver)
    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    inbound = channels.Inbound(
        token="C1:1.0",
        text="first",
        state={"team_id": "T1", "user_id": "U1"},
    )
    await server.bot.hub.dispatch("slack", inbound)
    [chat] = await chats.list_all()
    await chats.set_archived(chat.id, True)
    before = await events.read(chat.id, "messages")

    await server.bot.hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="blocked",
            state={"team_id": "T1", "user_id": "U1"},
        ),
    )

    assert await events.read(chat.id, "messages") == before
    assert len(started) == 1
    assert delivered == [
        (
            chat.id,
            "This chat is archived. Unarchive it in Hatchery before posting.",
            True,
        )
    ]


async def test_archived_chat_rejects_manual_sandbox(monkeypatch):
    chat = await chats.create(None, "archived", user_id="user_test")
    await chats.set_archived(chat.id, True)
    created = []

    async def create(*args):
        created.append(args)

    monkeypatch.setattr(server.sandbox, "create", create)
    async with client() as c:
        response = await c.post(
            f"/api/chats/{chat.id}/sandboxes",
            json={},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "chat is archived; unarchive it before creating a sandbox"
    }
    assert created == []


async def test_hub_lands_inbound_in_one_chat(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    delivered = []
    started = []

    async def start_turn(chat_id, origin, task_id=None):
        started.append((chat_id, origin, task_id))
        return f"run_{len(started)}"

    async def emit(chat_id, event):
        delivered.append((chat_id, event.type))
        return []

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_emit", emit)

    hub = server.bot.hub
    await hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="from slack",
            state={"channel_id": "C1", "team_id": "T1", "user_id": "U1"},
            title="a thread",
        ),
    )
    await hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="again",
            state={"team_id": "T1", "user_id": "U1"},
        ),
    )
    [chat] = await chats.list_all()
    assert chat.trigger == "slack:T1:C1:1.0"
    assert chat.title == "a thread"
    assert chat.author_display_name == "test@vercel.com"
    stored = await events.read(chat.id, "messages")
    assert len(stored) == 2
    assert delivered == [
        (chat.id, channels.protocol.SPACE_ASSIGNING),
        (chat.id, channels.protocol.SPACE_ASSIGNED),
    ]
    assert started == [
        (chat.id, "channel", None),
        (chat.id, "channel", None),
    ]


async def test_slack_threads_are_scoped_by_workspace(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    async def slack_user(team_id, _slack_user_id):
        return f"user_{team_id}"

    runs = []

    async def run(chat_id):
        runs.append(chat_id)

    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    for team_id in ("T1", "T2"):
        await server.bot.hub.dispatch(
            "slack",
            channels.Inbound(
                token="C1:1.0",
                text=f"from {team_id}",
                state={"team_id": team_id, "user_id": "U1"},
            ),
        )

    found = await chats.list_all()
    assert {chat.trigger for chat in found} == {
        "slack:T1:C1:1.0",
        "slack:T2:C1:1.0",
    }
    assert {chat.user_id for chat in found} == {"user_T1", "user_T2"}
    assert len(runs) == 2


async def test_slack_legacy_binding_rejects_different_participant(monkeypatch):
    legacy, _ = await chats.claim(
        "slack:C1:1.0",
        "slack",
        None,
        "legacy",
        {"team_id": "T1", "user_id": "U1"},
    )

    async def slack_user(_team_id, _slack_user_id):
        return "user_2"

    runs = []

    async def run(chat_id):
        runs.append(chat_id)

    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    await server.bot.hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="takeover",
            state={"team_id": "T1", "user_id": "U2"},
        ),
    )

    assert (await chats.get(legacy.id)).user_id is None
    assert await events.read(legacy.id, "messages") == []
    assert runs == []
    [binding] = await chats.bindings(legacy.id)
    assert binding.token == "slack:C1:1.0"


async def test_channel_hub_ignores_linked_but_disallowed_sender(monkeypatch):
    async def get_user(_user_id):
        return {"id": "user_test", "email": "removed@vercel.com"}

    monkeypatch.setattr(server.connections.auth_store, "get_user", get_user)

    await server.bot.hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="revoked",
            state={"team_id": "T1", "user_id": "U1"},
        ),
    )

    assert await chats.list_all() == []


async def test_slack_hub_ignores_unconnected_sender(monkeypatch):
    async def slack_user(team_id, slack_user_id):
        assert (team_id, slack_user_id) == ("T1", "U1")
        return None

    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)

    await server.bot.hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="unconnected",
            state={"team_id": "T1", "user_id": "U1"},
        ),
    )

    assert await chats.list_all() == []


async def test_slack_hub_rejects_takeover_without_appending_or_invoking(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    owners = iter(["user_1", "user_2"])

    async def slack_user(_team_id, _slack_user_id):
        return next(owners)

    runs = []

    async def run(chat_id):
        runs.append(chat_id)

    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    first = channels.Inbound(
        token="C1:1.0",
        text="first",
        state={"team_id": "T1", "user_id": "U1"},
    )
    second = channels.Inbound(
        token="C1:1.0",
        text="takeover",
        state={"team_id": "T1", "user_id": "U2"},
    )

    await server.bot.hub.dispatch("slack", first)
    [chat] = await chats.list_all()
    await server.bot.hub.dispatch("slack", second)

    assert chat.user_id == "user_1"
    assert len(await events.read(chat.id, "messages")) == 1
    assert runs == [chat.id]
    [binding] = await chats.bindings(chat.id)
    assert binding.state["user_id"] == "U1"


async def test_hub_can_store_without_invoking_then_wake_without_persisting(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    runs = []

    async def run(chat_id):
        runs.append(chat_id)

    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    hub = server.bot.hub
    await hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="context",
            state={"team_id": "T1", "user_id": "U1"},
            invoke=False,
        ),
    )
    [chat] = await chats.list_all()
    await hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="context",
            state={"team_id": "T1", "user_id": "U1"},
            persist=False,
        ),
    )

    stored = await events.read(chat.id, "messages")
    assert len(stored) == 1
    assert await events.read(chat.id, "ui") == [(0, {"type": "messages.changed"})]
    assert runs == [chat.id]


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
            state={"kind": "issue", "sender": "octocat", "sender_id": "42"},
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
                "channel_state": {"kind": "issue", "sender": "octocat", "sender_id": "42"},
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


async def test_inbound_turn_starts_durable_workflow(monkeypatch):
    started = []

    async def start_turn(chat_id, origin, task_id=None):
        started.append((chat_id, origin, task_id))
        return "run_1"

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    await server._run_inbound_turn("chat_x")

    assert started == [("chat_x", "channel", None)]


async def test_inbound_turn_surfaces_workflow_start_failure(monkeypatch):
    async def start_turn(chat_id, origin, task_id=None):
        raise RuntimeError("workflow unavailable")

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    with pytest.raises(RuntimeError, match="workflow unavailable"):
        await server._run_inbound_turn("chat_x")


async def test_hub_dedupe_is_durable():
    hub = server.bot.hub
    assert await hub.dedupe("slack:ev1") is True
    assert await hub.dedupe("slack:ev1") is False


async def test_slack_webhook_starts_durable_dispatcher_turn(monkeypatch):
    slack_channel = server.bot.channels["slack"]
    delivered = []
    started = []

    async def verify(headers):
        return None

    async def start_turn(chat_id, origin, task_id=None):
        started.append((chat_id, origin, task_id))
        return "run_1"

    async def classify(prompt, metadata, candidates):
        return candidates[0]

    async def on_event(event, state):
        delivered.append((event, state))

    monkeypatch.setattr(server.slack.connect, "verify_connect_webhook", verify)
    monkeypatch.setattr(server.durable, "start_turn", start_turn)
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
    assert started == [(chat.id, "channel", None)]
    assert [event.type for event, _ in delivered] == [
        channels.protocol.SPACE_ASSIGNING,
        channels.protocol.SPACE_ASSIGNED,
    ]


async def test_resume_chat_stream_is_idle_without_active_turn():
    space = await server.spaces.default()
    chat = await chats.create(space.id, "idle")

    async with client() as c:
        response = await c.get(f"/api/chat/{chat.id}/stream")

    assert response.status_code == 204


async def test_resume_chat_stream_uses_registered_run(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "active")
    await events.append(
        chat.id,
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_1",
            "run_id": "run_1",
            "origin": "worker",
            "task_id": "task_1",
        },
    )
    seen = []

    class Run:
        async def status(self):
            return "running"

    monkeypatch.setattr(server.vercel.workflow, "Run", lambda _run_id: Run())

    async def to_sse(run_id, turn_id):
        seen.append((run_id, turn_id))
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(server.agent_stream, "to_sse", to_sse)
    async with client() as c:
        response = await c.get(f"/api/chat/{chat.id}/stream")

    assert response.status_code == 200
    assert response.text == "data: [DONE]\n\n"
    assert seen == [("run_1", "turn_1")]


async def test_first_ui_prompt_classifies_before_dispatcher(monkeypatch):
    docs = await server.spaces.create("docs")
    docs.repos = ["vercel/docs"]
    await server.spaces.save(docs)
    chat = await chats.create(None, "new chat")
    seen = {}

    async def classify(prompt, metadata, candidates):
        seen["classification"] = (prompt, metadata, [space.id for space in candidates])
        return docs

    async def start_turn(chat_id, origin, task_id=None):
        seen["started"] = (chat_id, origin, task_id)
        return server.turns.ActiveTurn("turn_1", "run_1", origin, task_id, 0)

    async def durable_sse(run_id, turn_id):
        seen["stream"] = (run_id, turn_id)
        yield 'data: {"type":"finish"}\n\n'

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
    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", durable_sse)
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
    assert seen["started"] == (chat.id, "ui", None)
    assert seen["stream"] == ("run_1", "turn_1")
    assert response.text == 'data: {"type":"finish"}\n\n'


async def test_ui_turn_is_mirrored_to_bound_channel(monkeypatch):
    class FakeChannel:
        name = "fake"

        def __init__(self):
            self.delivered = []

        async def on_event(self, event, state):
            self.delivered.append((event, state))

    class FakeRun:
        def __init__(self, history):
            self.messages = [
                *history,
                ai.assistant_message("I will handle that."),
                ai.assistant_message("answer from AI"),
            ]

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

    async def start_turn(chat_id, origin, task_id=None):
        return server.turns.ActiveTurn("turn_1", "run_1", origin, task_id, 0)

    async def durable_sse(_run_id, _turn_id):
        yield 'data: {"type":"finish"}\n\n'

    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", durable_sse)
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
        ]
        assert channel.delivered[0][0].data == {
            "message": "continue in UI",
            "origin": "ui",
        }
        assert channel.delivered[0][1] == {"thread": "1"}
        stored = [
            ai.messages.Message.model_validate(data)
            for _, data in await events.read(chat.id, "messages")
        ]
        assert [(message.role, message.text) for message in stored] == [
            ("user", "continue in UI"),
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
        sandbox_name = "hatchery-wrk_1"

        def model_dump(self, exclude=None):
            assert exclude == {"daemon_token"}
            return {
                "id": self.id,
                "chat_id": chat.id,
                "title": "sandbox",
                "status": "running",
            }

    async def list_all(chat_id):
        seen["listed"] = chat_id
        return [Record()]

    async def create(chat_id, launch):
        seen["created"] = (chat_id, launch)
        return Record()

    async def is_live(name):
        seen["liveness"] = name
        return True

    task = server.worker.Task(
        id="task_1", chat_id=chat.id, worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test", fx_session_id="fx_1",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    monkeypatch.setattr(server.sandbox, "list_all", list_all)
    monkeypatch.setattr(server.sandbox, "create", create)
    monkeypatch.setattr(server.worker.sandbox, "is_live", is_live)

    async with client() as c:
        listed = await c.get(f"/api/chats/{chat.id}/sandboxes")
        created = await c.post(
            f"/api/chats/{chat.id}/sandboxes",
            json={
                "title": "sandbox", "repos": [], "setup_script": None,
                "ports": [], "branch": None, "git_sha": None,
            },
        )
        invalid_size = await c.post(
            f"/api/chats/{chat.id}/sandboxes",
            json={
                "title": "sandbox", "repos": [], "setup_script": None,
                "ports": [], "branch": None, "git_sha": None, "size": "medium",
            },
        )
        old = await c.get(f"/api/chats/{chat.id}/devboxes")

    assert listed.status_code == 200
    assert created.status_code == 200
    assert invalid_size.status_code == 422
    assert listed.json()[0]["id"] == "wrk_1"
    assert listed.json()[0]["status"] == "running"
    assert listed.json()[0]["live"] is True
    assert listed.json()[0]["subagents"][0]["task_id"] == "task_1"
    assert listed.json()[0]["subagents"][0]["fx_session_id"] == "fx_1"
    assert created.json()["id"] == "wrk_1"
    assert seen["listed"] == chat.id
    assert seen["liveness"] == "hatchery-wrk_1"
    assert seen["created"][0] == chat.id
    assert seen["created"][1].size == "small"
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
    started = []

    async def start_turn(chat_id, origin, task_id=None):
        started.append((chat_id, origin, task_id))
        return server.turns.ActiveTurn("turn_1", "run_1", origin, task_id, 0)

    class Run:
        async def status(self):
            return "running"

    monkeypatch.setattr(server.vercel.workflow, "Run", lambda run_id: Run())
    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    await server.complete_worker_task(task)
    await server.complete_worker_task(task)

    stored = [ai.messages.Message.model_validate(data) for _, data in await events.read(chat.id, "messages")]
    assert [(message.role, message.text) for message in stored] == [
        (
            "user",
            '<subagent_result>\n{"subagent_id":"task_1","status":"complete","result":{"summary":"fixed and tested"}}\n</subagent_result>',
        ),
    ]
    assert stored[0].provider_metadata == {
        "hatchery": {"kind": "subagent_result", "subagent_id": "task_1"}
    }
    assert started == [(chat.id, "worker", task.id)]
    current = await server.worker.get_task(chat.id, task.id)
    assert current.completion_run_id == "run_1"
    assert current.completion_delivered is False

    async with client() as c:
        visible = (await c.get(f"/api/chats/{chat.id}/messages")).json()
    assert visible == []


async def test_worker_completion_does_not_restart_active_workflow(monkeypatch):
    space = await server.spaces.default()
    chat = await chats.create(space.id, "task")
    task = server.worker.Task(
        id="task_1", chat_id=chat.id, worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test", status="complete", event_sequence=2,
        completion_sequence=2, completion_run_id="run_1",
        result={"summary": "done"}, created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    starts = []

    class Run:
        async def status(self):
            return "running"

    async def start_turn(*args):
        starts.append(args)
        return "run_2"

    monkeypatch.setattr(server.vercel.workflow, "Run", lambda run_id: Run())
    monkeypatch.setattr(server.durable, "start_turn", start_turn)
    await server.complete_worker_task(task)

    assert starts == []
    assert (await server.worker.get_task(chat.id, task.id)).completion_run_id == "run_1"


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


async def test_task_tty_bridges_pending_subagent_with_daemon_session(monkeypatch):
    task = server.worker.Task(
        id="task_1", chat_id="chat_1", worker_id="wrk_1", title="fix",
        prompt="fix it", model="openai/test",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    record = type("Worker", (), {"id": "wrk_1"})()
    daemon_sessions = {"task_1"}
    bridged = []

    async def get_task(chat_id, task_id):
        assert (chat_id, task_id) == ("chat_1", "task_1")
        return task

    async def get_worker(worker_id):
        assert worker_id == "wrk_1"
        return record

    async def bridge(ws, found, session_id):
        assert found is record
        assert session_id in daemon_sessions
        bridged.append((ws, session_id))

    monkeypatch.setattr(server.worker, "get_task", get_task)
    monkeypatch.setattr(server.worker, "get", get_worker)
    monkeypatch.setattr(server, "_bridge_tty", bridge)
    ws = FakeWebSocket()

    await server.task_tty(ws, "chat_1", "task_1")

    assert bridged == [(ws, "task_1")]
    assert ws.closed is None


async def test_task_tty_bridges_running_and_pending_subagents_to_their_sessions(
    monkeypatch,
):
    tasks = {
        "task_1": server.worker.Task(
            id="task_1", chat_id="chat_1", worker_id="wrk_1", title="first",
            prompt="first task", model="openai/test", status="running",
            created_at="2026-08-31T00:00:00+00:00",
            updated_at="2026-08-31T00:00:01+00:00",
        ),
        "task_2": server.worker.Task(
            id="task_2", chat_id="chat_1", worker_id="wrk_1", title="second",
            prompt="second task", model="openai/test",
            created_at="2026-08-31T00:00:02+00:00",
            updated_at="2026-08-31T00:00:02+00:00",
        ),
    }
    record = type("Worker", (), {"id": "wrk_1"})()
    daemon_sessions = set(tasks)
    bridged = []

    async def get_task(chat_id, task_id):
        assert chat_id == "chat_1"
        return tasks.get(task_id)

    async def get_worker(worker_id):
        assert worker_id == "wrk_1"
        return record

    async def bridge(ws, found, session_id):
        assert found is record
        assert session_id in daemon_sessions
        bridged.append(session_id)

    monkeypatch.setattr(server.worker, "get_task", get_task)
    monkeypatch.setattr(server.worker, "get", get_worker)
    monkeypatch.setattr(server, "_bridge_tty", bridge)

    await server.task_tty(FakeWebSocket(), "chat_1", "task_1")
    await server.task_tty(FakeWebSocket(), "chat_1", "task_2")

    assert bridged == ["task_1", "task_2"]


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
            self.messages = [
                *history,
                ai.assistant_message("I will inspect that."),
                ai.assistant_message("done"),
            ]

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

    assert await server._run_dispatcher_turn(chat.id, {"id": chat.id}) == [
        "I will inspect that.",
        "done",
    ]
    flush.assert_called_once_with()
