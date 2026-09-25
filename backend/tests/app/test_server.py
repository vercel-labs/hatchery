import asyncio
import datetime

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.frames import Close
from websockets.http11 import Response

import ai
import ai.experimental_telemetry
import ai.testing
from hatchery import channels
from hatchery import models
from hatchery.app import server
from hatchery.store import chats, events

from tests.agent.conftest import repo, run  # noqa: F401  (fixtures)


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
        protected = await c.get("/api/agents")
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

    assert await server._authenticate_websocket(ws) is None
    assert ws.closed == (4401, "sign in required")


async def test_browser_sees_unowned_channel_chat_without_claiming_it():
    chat, _ = await chats.claim("slack:C1:1.0", "slack", None, "slack", {})

    async with client() as c:
        listed = await c.get("/api/chats")
        direct = await c.get(f"/api/chats/{chat.id}/transcript")

    assert [item["id"] for item in listed.json()] == [chat.id]
    assert direct.status_code == 200
    assert (await chats.get(chat.id)).user_id is None


async def test_chat_routes_share_another_users_chat(monkeypatch):
    chat = await chats.create(None, "private", user_id="user_other")

    async with client() as c:
        listed = await c.get("/api/chats")
        direct = await c.get(f"/api/chats/{chat.id}/transcript")

    assert [item["id"] for item in listed.json()] == [chat.id]
    assert direct.status_code == 200


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
        authorized = await c.get(
            "/api/connections/github/authorize", follow_redirects=False
        )
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
        authorized = await c.get(
            "/api/connections/slack/authorize", follow_redirects=False
        )
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


async def test_agents_seed_default():
    async with client() as c:
        listed = (await c.get("/api/agents")).json()
    assert [s["id"] for s in listed] == ["hatchery"]


async def test_agent_warnings_check_main_repo_and_log(monkeypatch, caplog):
    agent = await server.agents.create("docs")
    agent.repos = ["acme/main", "acme/secondary"]
    await server.agents.save(agent)
    checked = []

    async def warning(user_id, repo):
        checked.append((user_id, repo))
        return "Install the Hatchery GitHub app on acme."

    monkeypatch.setattr(server.connections, "github_repo_warning", warning)

    with caplog.at_level("WARNING", logger="app"):
        async with client() as c:
            response = await c.get("/api/agents/warnings")

    assert response.json() == [
        {
            "agent_id": agent.id,
            "repo": "acme/main",
            "warning": "Install the Hatchery GitHub app on acme.",
        }
    ]
    assert checked == [("user_test", "acme/main")]
    assert "agent main repository lacks Hatchery GitHub access" in caplog.text


async def test_agent_create_and_delete():
    async with client() as c:
        created = await c.post("/api/agents", json={"name": "  docs  "})
        listed = (await c.get("/api/agents")).json()
        deleted = await c.delete(f"/api/agents/{created.json()['id']}")

    assert created.status_code == 200
    assert created.json()["name"] == "docs"
    assert created.json()["color"] in server.agents.ACCENT_COLORS
    assert [agent["id"] for agent in listed] == [created.json()["id"]]
    assert deleted.status_code == 204


async def test_agent_create_ids_and_rename_keeps_id():
    async with client() as c:
        derived = await c.post("/api/agents", json={"name": "Release Notes"})
        explicit = await c.post("/api/agents", json={"name": "Docs", "id": "docs-bot"})
        taken_explicit = await c.post(
            "/api/agents", json={"name": "Other", "id": "docs-bot"}
        )
        taken_derived = await c.post("/api/agents", json={"name": "release notes"})
        invalid = await c.post("/api/agents", json={"name": "Docs", "id": "Docs_Bot"})
        underivable = await c.post("/api/agents", json={"name": "!!!"})
        renamed = await c.patch(
            "/api/agents/docs-bot", json={"name": "Documentation"}
        )
        listed = (await c.get("/api/agents")).json()

    assert derived.status_code == 200
    assert derived.json()["id"] == "release-notes"
    assert explicit.json()["id"] == "docs-bot"
    assert explicit.json()["name"] == "Docs"
    assert taken_explicit.status_code == 409
    assert taken_derived.status_code == 409
    assert invalid.status_code == 422
    assert underivable.status_code == 422
    assert renamed.json()["id"] == "docs-bot"
    assert renamed.json()["name"] == "Documentation"
    assert [(agent["id"], agent["name"]) for agent in listed] == [
        ("release-notes", "Release Notes"),
        ("docs-bot", "Documentation"),
    ]


async def test_two_users_share_one_agent(monkeypatch):
    users = {
        "ada": {"id": "user_ada", "name": "Ada", "email": "test@vercel.com"},
        "bob": {"id": "user_bob", "name": "Bob", "email": "test@vercel.com"},
    }
    current = {"user": users["ada"]}
    started = []

    async def current_user(_request):
        return current["user"]

    async def start_turn(chat_id, origin, task_id=None, **kwargs):
        started.append((chat_id, kwargs["actor_user_id"]))
        return server.turns.ActiveTurn(
            "turn_1", "run_1", origin, task_id, 0, kwargs["actor_user_id"]
        )

    async def to_sse(_run_id, _turn_id):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(server.auth, "current_user", current_user)
    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", to_sse)

    async def chat_in(c, agent_id, text):
        chat = (await c.post("/api/chats", json={"agent_id": agent_id})).json()
        message = ai.ui.ai_sdk.to_ui_messages([ai.user_message(text)])[0]
        response = await c.post(
            "/api/chat",
            json={"chat_id": chat["id"], "messages": [message.model_dump(mode="json")]},
        )
        assert response.status_code == 200
        return chat

    async with client() as c:
        created = (await c.post("/api/agents", json={"name": "Release"})).json()
        ada_chat = await chat_in(c, created["id"], "draft notes")

        current["user"] = users["bob"]
        bob_listed = (await c.get("/api/agents")).json()
        bob_chat = await chat_in(c, created["id"], "add the fix")
        renamed = await c.patch(
            f"/api/agents/{created['id']}",
            json={"name": "Release train"},
        )

        current["user"] = users["ada"]
        ada_listed = (await c.get("/api/agents")).json()
        ada_renamed = await c.patch(
            f"/api/agents/{created['id']}",
            json={"name": "Releases"},
        )
        listed_chats = (await c.get("/api/chats")).json()

    assert created["id"] == "release"
    assert [agent["id"] for agent in bob_listed] == ["release"]
    assert renamed.status_code == 200
    assert [(agent["id"], agent["name"]) for agent in ada_listed] == [
        ("release", "Release train")
    ]
    assert ada_renamed.json()["id"] == "release"
    assert ada_renamed.json()["name"] == "Releases"
    assert started == [(ada_chat["id"], "user_ada"), (bob_chat["id"], "user_bob")]
    assert {chat["id"]: (chat["agent_id"], chat["user_id"]) for chat in listed_chats} == {
        ada_chat["id"]: ("release", "user_ada"),
        bob_chat["id"]: ("release", "user_bob"),
    }


async def test_agent_create_accepts_only_explicit_accent_ids():
    accent_colors = [
        f"{family}-{shade}"
        for family in ("blue", "red", "amber", "green", "teal", "purple", "pink")
        for shade in ("600", "700", "800", "900")
    ]
    async with client() as c:
        selected = [
            await c.post("/api/agents", json={"name": color, "color": color})
            for color in accent_colors
        ]
        updated = [
            await c.patch(
                f"/api/agents/{selected[0].json()['id']}",
                json={"name": "updated", "color": color},
            )
            for color in accent_colors
        ]
        bare = await c.post("/api/agents", json={"name": "bare", "color": "teal"})
        custom = await c.post(
            "/api/agents", json={"name": "custom", "color": "#38bdf8"}
        )

    assert [response.json()["color"] for response in selected] == accent_colors
    assert all(response.status_code == 200 for response in selected)
    assert [response.json()["color"] for response in updated] == accent_colors
    assert all(response.status_code == 200 for response in updated)
    assert bare.status_code == 422
    assert custom.status_code == 422


async def test_agent_delete_cascades_owner_scoped_jobs():
    agent = await server.agents.create("scheduled")
    own = await server.jobs.create(agent.id, "user_test", "0 9 * * *", "Mine")
    other = await server.jobs.create(agent.id, "user_other", "0 10 * * *", "Theirs")

    async with client() as c:
        response = await c.delete(f"/api/agents/{agent.id}")

    assert response.status_code == 204
    assert await server.jobs.get(own.id) is None
    assert await server.jobs.get(other.id) is None


async def test_agent_delete_rejects_unknown_agent_and_agent_with_chats():
    agent = await server.agents.create("busy")
    await chats.create(agent.id, "chat")

    async with client() as c:
        busy = await c.delete(f"/api/agents/{agent.id}")
        missing = await c.delete("/api/agents/missing")

    assert busy.status_code == 409
    assert busy.json() == {"detail": "agent still has chats"}
    assert missing.status_code == 404


async def test_agent_update():
    original = await server.agents.default()
    async with client() as c:
        response = await c.patch(
            "/api/agents/hatchery",
            json={"name": "  Hatchery docs  "},
        )
        listed = (await c.get("/api/agents")).json()

    assert response.status_code == 200
    assert response.json()["name"] == "Hatchery docs"
    assert response.json()["repos"] == original.repos
    assert response.json()["resources"] == [
        resource.model_dump() for resource in original.resources
    ]
    assert response.json()["color"] == original.color
    assert response.json()["created_at"] == original.created_at
    assert listed[0] == response.json()


async def test_agent_update_changes_accent_and_keeps_it_when_omitted():
    await server.agents.save(
        models.Agent(
            id="teal",
            name="Teal",
            color="teal-700",
            created_at="2026-09-08T00:00:00+00:00",
        )
    )

    async with client() as c:
        preserved = await c.patch("/api/agents/teal", json={"name": "Teal"})
        changed = await c.patch(
            "/api/agents/teal", json={"name": "Teal", "color": "pink-900"}
        )
        invalid = await c.patch(
            "/api/agents/teal", json={"name": "Teal", "color": "#fff"}
        )

    assert preserved.status_code == 200
    assert preserved.json()["color"] == "teal-700"
    assert changed.status_code == 200
    assert changed.json()["color"] == "pink-900"
    assert invalid.status_code == 422


async def test_agent_update_rejects_unknown_agent_and_empty_name():
    await server.agents.default()
    async with client() as c:
        missing = await c.patch(
            "/api/agents/missing", json={"name": "missing"}
        )
        invalid = await c.patch(
            "/api/agents/hatchery", json={"name": "   "}
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422


async def test_agent_resources_update():
    await server.agents.default()
    async with client() as c:
        response = await c.patch(
            "/api/agents/hatchery/resources",
            json={
                "repos": ["acme/app"],
                "resources": [
                    {"title": "docs", "url": "https://example.com/docs", "kind": "link"}
                ],
            },
        )
        listed = (await c.get("/api/agents")).json()

    assert response.status_code == 200
    assert response.json()["repos"] == ["acme/app"]
    assert response.json()["resources"] == [
        {"title": "docs", "url": "https://example.com/docs", "kind": "link"}
    ]
    assert listed[0]["resources"] == response.json()["resources"]


async def test_agent_resources_update_rejects_unknown_agent_and_invalid_repo():
    await server.agents.default()
    async with client() as c:
        missing = await c.patch(
            "/api/agents/missing/resources", json={"repos": [], "resources": []}
        )
        invalid = await c.patch(
            "/api/agents/hatchery/resources",
            json={"repos": ["https://github.com/acme/app"], "resources": []},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422


async def test_job_routes_are_owner_scoped():
    await server.agents.default()
    async with client() as c:
        created = await c.post(
            "/api/agents/hatchery/jobs",
            json={"schedule": "0 9 * * 1-5", "prompt": "Check reports"},
            headers={"origin": "http://test"},
        )
        listed = await c.get("/api/agents/hatchery/jobs")
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
    assert created.json()["author_display_name"] == "test@vercel.com"
    assert listed.json() == [created.json()]
    assert paused.json()["paused"] is True
    assert invalid.status_code == 422
    assert deleted.status_code == 204


async def test_cron_heartbeat_auth_and_reconciliation(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-test-secret")
    agent = await server.agents.default()
    job = await server.jobs.create(
        agent.id,
        "user_test",
        "* * * * *",
        "Do work",
        author_display_name="Ada Lovelace",
    )
    job.next_run_at = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=2)
    ).isoformat()
    server.jobs._write_job(job)
    starts = []

    async def start_turn(
        chat_id, origin, task_id=None, turn_id=None, actor_user_id=None
    ):
        starts.append((chat_id, origin, turn_id, actor_user_id))
        await server.jobs.claim_run(turn_id, "run_1")
        return server.turns.ActiveTurn(turn_id, "run_1", origin, task_id, 0)

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    async with client() as c:
        denied = await c.get("/api/cron")
        first = await c.get(
            "/api/cron", headers={"authorization": "Bearer cron-test-secret"}
        )
        if server._background:
            await asyncio.gather(*list(server._background))
        duplicate = await c.get(
            "/api/cron", headers={"authorization": "Bearer cron-test-secret"}
        )
        visible = await c.get("/api/chats")
        [message] = (await c.get(f"/api/chats/{starts[0][0]}/transcript")).json()

    assert denied.status_code == 401
    assert first.json() == {"ok": True, "started": 1}
    assert duplicate.json() == {"ok": True, "started": 0}
    assert len(starts) == 1
    assert starts[0][1] == "cron"
    assert starts[0][3] == "user_test"
    assert visible.json()[0]["trigger"] == f"cron:{job.id}"
    assert visible.json()[0]["author_display_name"] == "Ada Lovelace"
    assert visible.json()[0]["topic"] == "Test request"
    assert (message["origin"], message["author"]) == ("cron", "Ada Lovelace")
    transcript = await server._transcript(starts[0][0])
    assert [message.text for message in transcript] == ["Do work"]


async def test_paused_pending_job_does_not_start(monkeypatch):
    secret = "cron-test-secret"
    monkeypatch.setenv("CRON_SECRET", secret)
    agent = await server.agents.default()
    job = await server.jobs.create(agent.id, "user_test", "* * * * *", "Do work")
    now = datetime.datetime.now(datetime.UTC)
    job.next_run_at = (now - datetime.timedelta(minutes=1)).isoformat()
    server.jobs._write_job(job)
    await server.jobs.claim_due(now)
    await server.jobs.set_paused(job.id, True)
    starts = []

    async def start_turn(*args, **kwargs):
        starts.append((args, kwargs))

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
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
        assert created["agent_id"] is None
        assert created["title"] == "new chat"
        assert created["author_display_name"] == "Ada Lovelace"
        listed = (await c.get("/api/chats")).json()
    assert [x["id"] for x in listed] == [created["id"]]
    assert listed[0]["author_display_name"] == "Ada Lovelace"


async def test_chat_create_retries_one_client_generated_id():
    request = {"id": "chat_123456789abc", "agent_id": None}
    async with client() as c:
        first = await c.post("/api/chats", json=request)
        second = await c.post("/api/chats", json=request)
        listed = (await c.get("/api/chats")).json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert [item["id"] for item in listed] == ["chat_123456789abc"]


async def test_chat_create_rejects_conflicting_client_generated_id():
    await chats.create_once("chat_123456789abc", None, "new chat", user_id="user_other")
    async with client() as c:
        response = await c.post("/api/chats", json={"id": "chat_123456789abc"})

    assert response.status_code == 409
    assert response.json() == {"detail": "chat id conflicts with an existing chat"}


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

    monkeypatch.setattr(server.supervisor, "active_turn", active_turn)
    async with client() as c:
        response = await c.patch(
            f"/api/chats/{chat.id}/archive",
            json={"archived": True},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "chat has an active turn"}
    assert (await chats.get(chat.id)).archived_at is None


async def test_mark_chat_seen_clears_attention_and_notifies():
    chat = await chats.create(None, "work", user_id="user_test")
    await chats.set_attention(chat.id, "blocked")

    async with client() as c:
        loaded = await c.get(f"/api/chats/{chat.id}/transcript")
        still_required = await chats.get(chat.id)
        response = await c.post(
            f"/api/chats/{chat.id}/seen", headers={"origin": "http://test"}
        )

    assert loaded.status_code == 200
    assert still_required is not None and still_required.attention_reason == "blocked"
    assert response.status_code == 200
    assert response.json()["attention_reason"] is None
    assert (await chats.get(chat.id)).attention_reason is None
    assert await events.read(chat.id, "ui") == [(0, {"type": "chat.changed"})]


async def test_mark_chat_seen_is_shared_but_authenticated(monkeypatch):
    chat = await chats.create(None, "work", user_id="user_other")
    await chats.set_attention(chat.id, "result_available")

    async with client() as c:
        hidden = await c.post(
            f"/api/chats/{chat.id}/seen", headers={"origin": "http://test"}
        )

    async def current_user(_request):
        return None

    monkeypatch.setattr(server.auth, "current_user", current_user)
    async with client() as c:
        unauthenticated = await c.post(
            f"/api/chats/{chat.id}/seen", headers={"origin": "http://test"}
        )

    assert hidden.status_code == 200
    assert unauthenticated.status_code == 401
    assert (await chats.get(chat.id)).attention_reason is None


async def test_chat_agent_assignment():
    destination = await server.agents.create("docs")
    chat = await chats.create(None, "work")

    async with client() as c:
        response = await c.patch(
            f"/api/chats/{chat.id}/agent", json={"agent_id": destination.id}
        )
        listed = (await c.get("/api/chats")).json()

    assert response.status_code == 200
    assert response.json()["agent_id"] == destination.id
    assert listed[0]["agent_id"] == destination.id
    assert (await server._agent_for_chat(chat.id)).id == destination.id


async def test_chat_agent_is_fixed_once_its_thread_started():
    destination = await server.agents.create("docs")
    chat = await chats.create(None, "work")
    await events.append(
        chat.id, "thread", {"agent_id": "hatchery", "thread_id": "thread_1", "cursor": 0}
    )
    async with client() as c:
        response = await c.patch(
            f"/api/chats/{chat.id}/agent", json={"agent_id": destination.id}
        )
    assert response.status_code == 409
    assert (await chats.get(chat.id)).agent_id is None


async def test_chat_list_shows_root_chats_and_children_through_their_parent():
    root = await chats.create("hatchery", "root")
    child = await chats.create_once(
        "chat_0123456789ab", "hatchery", "child task", None, trigger="task",
        parent_chat_id=root.id,
    )
    async with client() as c:
        roots = (await c.get("/api/chats")).json()
        children = (await c.get("/api/chats", params={"parent_chat_id": root.id})).json()
    assert [item["id"] for item in roots] == [root.id]
    assert [item["id"] for item in children] == [child.id]
    assert children[0]["parent_chat_id"] == root.id


async def test_agent_create_commits_its_template(monkeypatch, tmp_path):
    from hatchery import config, environment, templates
    from hatchery.worker import scripted
    from hatchery.workspace import local, repo, review

    root = tmp_path / "storage"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "PROMPT.md").write_text("Team prompt\n")
    workspaces = repo.WorkspaceRepo(await local.initialize_local(root))
    env = environment.Environment(
        config.Config(),
        workspaces,
        review.LocalReview(workspaces),
        scripted.ScriptedSandboxProvider(),
        ai.get_model("openai/gpt-5.6-sol"),
    )
    monkeypatch.setattr(server.rotor_runtime, "install", lambda: env)
    async with client() as c:
        created = await c.post(
            "/api/agents", json={"name": "Docs"}
        )
    assert created.status_code == 200
    _, files = await workspaces.read_main("docs")
    assert files["self/AGENTS.md"].content == templates.load()["AGENTS.md"].content
    assert "self/MEMORY.md" in files


async def test_default_agent_seed_commits_the_template_once_and_retries_after_failure(
    monkeypatch, tmp_path
):
    from hatchery import config, environment, templates
    from hatchery.worker import scripted
    from hatchery.workspace import git, local, repo, review

    # A fresh storage main: no agents/ and no wiki/ yet.
    root = tmp_path / "storage"
    root.mkdir()
    (root / "README.md").write_text("storage\n")
    workspaces = repo.WorkspaceRepo(await local.initialize_local(root))
    env = environment.Environment(
        config.Config(),
        workspaces,
        review.LocalReview(workspaces),
        scripted.ScriptedSandboxProvider(),
        ai.get_model("openai/gpt-5.6-sol"),
    )
    monkeypatch.setattr(server.rotor_runtime, "install", lambda: env)
    join = workspaces.join
    failures = [git.GitError("push rejected")]

    async def flaky_join(owner, workspace):
        if failures:
            raise failures.pop()
        return await join(owner, workspace)

    monkeypatch.setattr(workspaces, "join", flaky_join)

    async with client() as c:
        with pytest.raises(git.GitError):
            await c.get("/api/agents")
        assert await server.agents.get(server.agents.DEFAULT_ID) is None, "retryable"

        listed = await c.get("/api/agents")
        assert [agent["id"] for agent in listed.json()] == ["hatchery"]
        revision, files = await workspaces.read_main("hatchery")
        assert files["self/AGENTS.md"] == templates.load()["AGENTS.md"]

        # Seeded once: later calls find the row and commit nothing.
        await c.get("/api/agents")
        assert (await workspaces.read_main("hatchery"))[0] == revision

        # A commit that landed without its row is found again, not duplicated.
        await server.agents.delete(server.agents.DEFAULT_ID)
        listed = await c.get("/api/agents")
        assert [agent["id"] for agent in listed.json()] == ["hatchery"]
        assert (await workspaces.read_main("hatchery"))[0] == revision


async def test_grant_and_thread_tree_go_to_the_agent_supervisor(monkeypatch):
    sent = []

    async def send(agent_id, msg, *, idempotency_key=None):
        sent.append((agent_id, msg, idempotency_key))
        return "delivered"

    async def roster(agent_id):
        return {"agent_id": agent_id, "threads": [], "budget": {"remaining": 5}}

    monkeypatch.setattr(server.supervisor, "send", send)
    monkeypatch.setattr(server.supervisor, "roster", roster)
    await server.agents.default()
    async with client() as c:
        granted = await c.post(
            "/api/agents/hatchery/grants", json={"request_id": "g1", "amount": 500}
        )
        tree = await c.get("/api/agents/hatchery/threads")
        missing = await c.get("/api/chats/chat_missing/thread")
    assert granted.status_code == 202
    assert sent[0][0] == "hatchery" and sent[0][2] == "grant:g1"
    assert (sent[0][1].id, sent[0][1].amount) == ("g1", 500)
    assert tree.json()["budget"] == {"remaining": 5}
    assert missing.status_code == 404


async def test_chat_agent_assignment_rejects_unknown_chat_agent_and_null():
    destination = await server.agents.create("docs")
    chat = await chats.create(destination.id, "work")
    async with client() as c:
        missing_chat = await c.patch(
            "/api/chats/chat_missing/agent", json={"agent_id": destination.id}
        )
        missing_agent = await c.patch(
            "/api/chats/chat_missing/agent", json={"agent_id": "missing"}
        )
        null_agent = await c.patch(
            f"/api/chats/{chat.id}/agent", json={"agent_id": None}
        )

    assert missing_chat.status_code == 404
    assert missing_chat.json() == {"detail": "unknown chat"}
    assert missing_agent.status_code == 404
    assert missing_agent.json() == {"detail": "unknown agent"}
    assert null_agent.status_code == 422
    assert (await chats.get(chat.id)).agent_id == destination.id


async def test_name_chat_generates_and_persists_topic(monkeypatch):
    chat = await chats.create(None, "new chat")

    async def generate(prompt):
        assert prompt == "Improve chat names"
        return "Sidebar chat names"

    monkeypatch.setattr(server.topic, "generate", generate)
    sink = ai.experimental_telemetry.DictSink()
    async with ai.experimental_telemetry.use_sink(sink):
        await server._name_chat(chat.id, "Improve chat names")

    root = next(span for span in sink.finished_spans if span.name == "hatchery.chat")
    title = next(span for span in sink.finished_spans if span.name == "hatchery.title")
    assert title.trace_id == root.trace_id
    assert title.parent_id == root.id
    named = await chats.get(chat.id)
    assert named is not None and named.topic == "Sidebar chat names"
    assert await events.read(chat.id, "ui") == [(0, {"type": "chat.changed"})]


async def test_chat_events_replay_after_cursor():
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "events")
    await events.append(chat.id, "ui", {"type": "old"})
    await events.append(chat.id, "ui", {"type": "messages.changed"})

    request = httpx.Request("GET", f"http://test/api/chats/{chat.id}/events")
    response = await server.chat_events(chat.id, request, after=0)
    chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert chunk == 'id: 1\ndata: {"type":"messages.changed"}\n\n'


async def test_chat_transcript_from_store():
    chat = await chats.create(None, "messages", user_id="user_test")
    for message in (ai.user_message("hi"), ai.assistant_message("hello")):
        await events.append(chat.id, "messages", message.model_dump(mode="json"))
    async with client() as c:
        ui = (await c.get(f"/api/chats/{chat.id}/transcript")).json()
        missing = await c.get("/api/chats/chat_missing/transcript")
    assert [m["role"] for m in ui] == ["user", "assistant"]
    assert ui[0]["parts"][0]["text"] == "hi"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "unknown chat"}


async def test_chat_transcript_unwraps_channel_text_and_keeps_author_and_task_source():
    chat = await chats.create(None, "slack message", user_id="user_test")
    slack = ai.user_message(
        '<slack_message channel="C1" ts="1.1">\nhello &lt;-&gt; slack\n</slack_message>'
    )
    slack.provider_metadata = {"hatchery": {"author": "Ada"}}
    report = ai.user_message("Child finished the audit")
    await events.append(chat.id, "messages", slack.model_dump(mode="json"))
    await events.append(
        chat.id,
        "messages",
        {
            **report.model_dump(mode="json"),
            "timestamp": 5.0,
            "source": "task",
            "task_handle": "task-1",
            "task_event": "completion",
        },
    )
    async with client() as c:
        first, second = (await c.get(f"/api/chats/{chat.id}/transcript")).json()
        missing = await c.get("/api/chats/chat_missing/transcript")
    assert first["parts"][0]["text"] == "hello <-> slack"
    assert (first["origin"], first["author"]) == ("slack", "Ada")
    assert (second["source"], second["task_handle"], second["timestamp"]) == (
        "task",
        "task-1",
        5.0,
    )
    assert missing.status_code == 404


async def test_ui_post_uses_actor_in_another_users_chat(monkeypatch):
    agent = await server.agents.default()
    chat = await chats.create(
        agent.id, "shared", user_id="user_other", author_display_name="Creator"
    )
    await chats.set_topic(chat.id, "shared work")
    message = ai.ui.ai_sdk.to_ui_messages([ai.user_message("continue")])[0]

    async def start_turn(chat_id, origin, task_id=None, **kwargs):
        assert kwargs["actor_user_id"] == "user_test"
        return server.turns.ActiveTurn(
            "turn_1", "run_1", origin, task_id, 0, "user_test"
        )

    async def to_sse(_run_id, _turn_id):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", to_sse)
    async with client() as c:
        response = await c.post(
            "/api/chat",
            json={
                "chat_id": chat.id,
                "messages": [message.model_dump(mode="json")],
            },
        )

    assert response.status_code == 200
    [stored] = await server._transcript(chat.id)
    assert stored.provider_metadata["hatchery"] == {
        "origin": "ui",
        "author": "test@vercel.com",
        "actor_user_id": "user_test",
    }
    unchanged = await chats.get(chat.id)
    assert unchanged.user_id == "user_other"
    assert unchanged.author_display_name == "Creator"


async def test_archived_chat_rejects_ui_post_before_persisting(monkeypatch):
    chat = await chats.create(None, "archived", user_id="user_test")
    await chats.set_archived(chat.id, True)
    started = []

    async def start_turn(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
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

    async def start_turn(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_deliver", deliver)
    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
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


async def test_hub_lands_inbound_in_one_chat(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    delivered = []
    started = []

    async def start_turn(chat_id, origin, task_id=None, actor_user_id=None):
        started.append((chat_id, origin, task_id))
        return f"run_{len(started)}"

    async def emit(chat_id, event):
        delivered.append((chat_id, event.type))
        return []

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
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
        (chat.id, channels.protocol.MESSAGE_RECEIVED),
        (chat.id, channels.protocol.AGENT_ASSIGNING),
        (chat.id, channels.protocol.AGENT_ASSIGNED),
        (chat.id, channels.protocol.MESSAGE_RECEIVED),
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

    async def run(chat_id, actor_user_id=None):
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


async def test_slack_binding_accepts_allowed_participant(monkeypatch):
    async def classify(_prompt, _metadata, candidates):
        return candidates[0]

    monkeypatch.setattr(server.classifier, "classify", classify)
    first, _ = await chats.claim(
        "slack:T1:C1:1.0",
        "slack",
        None,
        "first",
        {"team_id": "T1", "user_id": "U1"},
    )

    async def slack_user(_team_id, _slack_user_id):
        return "user_2"

    runs = []

    async def run(chat_id, actor_user_id=None):
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

    assert (await chats.get(first.id)).user_id is None
    assert len(await events.read(first.id, "messages")) == 1
    assert runs == [first.id]
    [binding] = await chats.bindings(first.id)
    assert binding.token == "slack:T1:C1:1.0"


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


async def test_bound_thread_stores_unlinked_participant_without_invoking(monkeypatch):
    async def classify(_prompt, _metadata, candidates):
        return candidates[0]

    async def slack_user(_team_id, slack_user_id):
        return "user_test" if slack_user_id == "U1" else None

    runs = []

    async def run(chat_id, actor_user_id=None):
        runs.append((chat_id, actor_user_id))

    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    first = channels.Inbound(
        token="C1:1.0",
        text="start",
        state={"team_id": "T1", "user_id": "U1", "message_id": "1.0"},
    )
    await server.bot.hub.dispatch("slack", first)
    [chat] = await chats.list_all()

    await server.bot.hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text="participant context",
            state={"team_id": "T1", "user_id": "U2", "message_id": "1.1"},
        ),
    )

    assert len(await events.read(chat.id, "messages")) == 2
    assert runs == [(chat.id, "user_test")]
    assert (await chats.get(chat.id)).user_id == "user_test"


async def test_slack_hub_accepts_linked_participant_without_changing_owner(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    owners = iter(["user_1", "user_2"])

    async def slack_user(_team_id, _slack_user_id):
        return next(owners)

    runs = []

    async def run(chat_id, actor_user_id=None):
        runs.append(chat_id)

    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    first = channels.Inbound(
        token="C1:1.0",
        text="first",
        state={
            "team_id": "T1",
            "channel_id": "C1",
            "thread_ts": "1.0",
            "user_id": "U1",
        },
    )
    second = channels.Inbound(
        token="C1:1.0",
        text="follow up",
        state={
            "team_id": "T1",
            "channel_id": "C1",
            "thread_ts": "1.0",
            "user_id": "U2",
        },
    )

    await server.bot.hub.dispatch("slack", first)
    [chat] = await chats.list_all()
    await server.bot.hub.dispatch("slack", second)

    assert chat.user_id == "user_1"
    assert len(await events.read(chat.id, "messages")) == 2
    assert runs == [chat.id, chat.id]
    [binding] = await chats.bindings(chat.id)
    assert binding.state["user_id"] == "U2"


async def test_linked_message_syncs_to_other_channel_and_ui(monkeypatch):
    class FakeChannel:
        def __init__(self, name):
            self.name = name
            self.delivered = []

        async def on_event(self, event, state):
            self.delivered.append((event, state))

    async def slack_user(_team_id, _slack_user_id):
        return "user_2"

    async def get_user(_user_id):
        return {"id": "user_2", "name": "John Business", "email": "test@vercel.com"}

    async def run(_chat_id):
        raise AssertionError("an unaddressed follow-up must not invoke the thread")

    monkeypatch.setattr(server.connections.auth_store, "slack_user", slack_user)
    monkeypatch.setattr(server.connections.auth_store, "get_user", get_user)
    monkeypatch.setattr(server, "_run_inbound_turn", run)
    slack_channel = FakeChannel("slack")
    github_channel = FakeChannel("github")
    monkeypatch.setitem(server.bot.channels, "slack", slack_channel)
    monkeypatch.setitem(server.bot.channels, "github", github_channel)

    agent = await server.agents.default()
    chat = await chats.create(agent.id, "shared", user_id="user_1")
    await chats.bind(
        "slack:T1:C1:1.0",
        chat.id,
        "slack",
        {"team_id": "T1", "channel_id": "C1", "thread_ts": "1.0"},
    )
    await chats.bind(
        "github:repo:1:issue:7",
        chat.id,
        "github",
        {"owner": "acme", "repo": "repo", "kind": "issue", "number": 7},
    )

    await server.bot.hub.dispatch(
        "slack",
        channels.Inbound(
            token="C1:1.0",
            text='<slack_message sender="U2">follow up</slack_message>',
            state={
                "team_id": "T1",
                "channel_id": "C1",
                "thread_ts": "1.0",
                "user_id": "U2",
                "message_id": "1.1",
                "display_text": "follow up",
            },
            invoke=False,
        ),
    )

    assert slack_channel.delivered == []
    [(mirrored, _)] = github_channel.delivered
    assert mirrored.type == channels.protocol.MESSAGE_RECEIVED
    assert mirrored.data["message"] == "follow up"
    assert mirrored.data["author"] == "John Business"
    [stored] = [
        ai.messages.Message.model_validate(data)
        for _, data in await events.read(chat.id, "messages")
    ]
    assert stored.provider_metadata["hatchery"] == {
        "origin": "slack",
        "author": "John Business",
        "display_text": "follow up",
        "actor_user_id": "user_2",
    }


async def test_hub_can_store_without_invoking_then_wake_without_persisting(monkeypatch):
    async def classify(prompt, metadata, candidates):
        return candidates[0]

    runs = []

    async def run(chat_id, actor_user_id=None):
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
    first = await server.agents.create("docs")
    first.repos = ["vercel/repo"]
    await server.agents.save(first)
    second = await server.agents.create("release")
    second.repos = ["vercel/repo"]
    await server.agents.save(second)
    emitted = []
    runs = []
    classified = []

    async def emit(chat_id, event):
        emitted.append((chat_id, event))
        return []

    async def classify(prompt, metadata, candidates):
        classified.append((prompt, metadata, [agent.id for agent in candidates]))
        return second

    async def run(chat_id, actor_user_id=None):
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
    assert chat.agent_id == second.id
    assert classified == [
        (
            "fix the docs",
            {
                "origin": "github",
                "author": "test@vercel.com",
                "repo": "vercel/repo",
                "channel_state": {
                    "kind": "issue",
                    "sender": "octocat",
                    "sender_id": "42",
                },
            },
            [first.id, second.id],
        )
    ]
    assert [event.type for _, event in emitted] == [
        channels.protocol.MESSAGE_RECEIVED,
        channels.protocol.AGENT_ASSIGNING,
        channels.protocol.AGENT_ASSIGNED,
    ]
    assert emitted[-1][1].data["agent"]["name"] == "release"
    assert len(await events.read(chat.id, "messages")) == 1
    assert runs == [chat.id]


async def test_inbound_turn_starts_rotor_thread(monkeypatch):
    started = []

    async def start_turn(chat_id, origin, task_id=None, actor_user_id=None):
        started.append((chat_id, origin, task_id))
        return "run_1"

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    await server._run_inbound_turn("chat_x")

    assert started == [("chat_x", "channel", None)]


async def test_inbound_turn_surfaces_thread_start_failure(monkeypatch):
    async def start_turn(chat_id, origin, task_id=None, actor_user_id=None):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        await server._run_inbound_turn("chat_x")


async def test_hub_dedupe_is_durable():
    hub = server.bot.hub
    assert await hub.dedupe("slack:ev1") is True
    assert await hub.dedupe("slack:ev1") is False


async def test_slack_webhook_starts_agent_thread_turn(monkeypatch):
    slack_channel = server.bot.channels["slack"]
    delivered = []
    started = []

    async def verify(headers):
        return None

    async def start_turn(chat_id, origin, task_id=None, actor_user_id=None):
        started.append((chat_id, origin, task_id))
        return "run_1"

    async def classify(prompt, metadata, candidates):
        return candidates[0]

    async def on_event(event, state):
        delivered.append((event, state))

    monkeypatch.setattr(server.slack.connect, "verify_connect_webhook", verify)
    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
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
    sink = ai.experimental_telemetry.DictSink()
    async with ai.experimental_telemetry.use_sink(sink):
        async with client() as c:
            response = await c.post(
                "/channels/v1/slack",
                headers={"authorization": "Bearer good"},
                json=payload,
            )

    assert response.status_code == 200
    [chat] = await chats.list_all()
    root = next(span for span in sink.finished_spans if span.name == "hatchery.chat")
    unified = [
        span
        for span in sink.finished_spans
        if span.name in {"channel.dispatch", "hatchery.classify"}
    ]
    assert {span.trace_id for span in unified} == {root.trace_id}
    assert started == [(chat.id, "channel", None)]
    assert [event.type for event, _ in delivered] == [
        channels.protocol.AGENT_ASSIGNING,
        channels.protocol.AGENT_ASSIGNED,
    ]


async def test_resume_chat_stream_is_idle_without_active_turn():
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "idle")

    async with client() as c:
        response = await c.get(f"/api/chat/{chat.id}/stream")

    assert response.status_code == 204


async def test_resume_chat_stream_uses_registered_process(monkeypatch):
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "active")
    await events.append(
        chat.id,
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_1",
            "run_id": "process_1",
            "origin": "worker",
            "task_id": "task_1",
        },
    )
    seen = []

    async def active_turn(_chat_id):
        return server.turns.ActiveTurn(
            "turn_1", "process_1", "worker", "task_1", 0
        )

    async def to_sse(process_id, turn_id):
        seen.append((process_id, turn_id))
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(server.supervisor, "active_turn", active_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", to_sse)
    async with client() as c:
        response = await c.get(f"/api/chat/{chat.id}/stream")

    assert response.status_code == 200
    assert response.text == "data: [DONE]\n\n"
    assert seen == [("process_1", "turn_1")]


async def test_first_ui_prompt_classifies_before_thread(monkeypatch):
    docs = await server.agents.create("docs")
    docs.repos = ["vercel/docs"]
    await server.agents.save(docs)
    chat = await chats.create(None, "new chat")
    seen = {}

    async def classify(prompt, metadata, candidates):
        seen["classification"] = (prompt, metadata, [agent.id for agent in candidates])
        return docs

    async def start_turn(
        chat_id, origin, task_id=None, turn_id=None, actor_user_id=None
    ):
        seen["started"] = (chat_id, origin, task_id, turn_id, actor_user_id)
        return server.turns.ActiveTurn(
            turn_id, "run_1", origin, task_id, 0, actor_user_id
        )

    async def durable_sse(run_id, turn_id):
        seen["stream"] = (run_id, turn_id)
        yield 'data: {"type":"finish"}\n\n'

    monkeypatch.setattr(server.classifier, "classify", classify)
    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", durable_sse)
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
    assert (await chats.get(chat.id)).agent_id == docs.id
    assert seen["started"][:3] == (chat.id, "ui", None)
    assert seen["started"][3].startswith("turn_")
    assert seen["started"][4] == "user_test"
    assert seen["stream"] == ("run_1", seen["started"][3])
    assert response.text == 'data: {"type":"finish"}\n\n'


async def test_ui_turn_is_mirrored_to_bound_channel(monkeypatch):
    class FakeChannel:
        name = "fake"

        def __init__(self):
            self.delivered = []

        async def on_event(self, event, state):
            self.delivered.append((event, state))

    async def current_user(_request):
        return {
            "id": "user_actor",
            "email": "test@vercel.com",
            "name": "Actor",
            "slack": {"team_id": "T1", "user_id": "U1"},
        }

    monkeypatch.setattr(server.auth, "current_user", current_user)
    channel = FakeChannel()
    previous = server.bot.channels.get("fake")
    server.bot.channels["fake"] = channel

    async def start_turn(
        chat_id, origin, task_id=None, turn_id=None, actor_user_id=None
    ):
        assert actor_user_id == "user_actor"
        return server.turns.ActiveTurn(
            turn_id, "run_1", origin, task_id, 0, actor_user_id
        )

    async def durable_sse(_run_id, _turn_id):
        yield 'data: {"type":"finish"}\n\n'

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", durable_sse)
    try:
        agent = await server.agents.default()
        chat, _ = await chats.claim(
            "fake:thread",
            "fake",
            agent.id,
            "thread",
            {"thread": "1"},
            user_id="user_creator",
            author_display_name="Creator",
        )
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
            "message_id": ui[0].id,
            "origin": "ui",
            "author": "Actor",
            "slack_team_id": "T1",
            "slack_user_id": "U1",
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
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "task")
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

    async def is_live(name):
        seen["liveness"] = name
        return True

    task = server.worker.Task(
        id="task_1",
        chat_id=chat.id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        fx_session_id="fx_1",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    monkeypatch.setattr(server.sandbox, "list_all", list_all)
    monkeypatch.setattr(server.worker.sandbox, "is_live", is_live)

    async with client() as c:
        listed = await c.get(f"/api/chats/{chat.id}/sandboxes")

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "wrk_1"
    assert listed.json()[0]["status"] == "running"
    assert listed.json()[0]["live"] is True
    assert listed.json()[0]["subagents"][0]["task_id"] == "task_1"
    assert listed.json()[0]["subagents"][0]["fx_session_id"] == "fx_1"
    assert seen["listed"] == chat.id
    assert seen["liveness"] == "hatchery-wrk_1"


async def test_worker_completion_wakes_thread_with_hidden_persisted_result(
    monkeypatch,
):
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "task")
    task = server.worker.Task(
        id="task_1",
        chat_id=chat.id,
        worker_id="wrk_1",
        user_id="user_actor",
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

    async def start_turn(
        chat_id, origin, task_id=None, turn_id=None, actor_user_id=None
    ):
        item = (chat_id, origin, task_id, turn_id, actor_user_id)
        if item not in started:
            started.append(item)
        return server.turns.ActiveTurn(
            turn_id, "process_1", origin, task_id, 0, actor_user_id
        )

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    await server.complete_worker_task(task)
    await server.complete_worker_task(task)

    stored = [
        ai.messages.Message.model_validate(data)
        for _, data in await events.read(chat.id, "messages")
    ]
    assert [(message.role, message.text) for message in stored] == [
        (
            "user",
            '<subagent_result>\n{"subagent_id":"task_1","status":"complete","result":{"summary":"fixed and tested"}}\n</subagent_result>',
        ),
    ]
    assert stored[0].provider_metadata == {
        "hatchery": {"kind": "subagent_result", "subagent_id": "task_1"}
    }
    assert len(started) == 1
    assert started[0][:3] == (chat.id, "worker", task.id)
    assert started[0][3].startswith("turn_")
    assert started[0][4] == "user_actor"
    current = await server.worker.get_task(chat.id, task.id)
    assert current.completion_delivered is False

    async with client() as c:
        visible = (await c.get(f"/api/chats/{chat.id}/transcript")).json()
    assert visible == []


async def test_worker_completion_reuses_stable_rotor_turn_id(monkeypatch):
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "task")
    task = server.worker.Task(
        id="task_1",
        chat_id=chat.id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        status="complete",
        event_sequence=2,
        completion_sequence=2,
        result={"summary": "done"},
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    starts = []

    async def start_turn(*args, **kwargs):
        starts.append((args, kwargs))
        return "process_1"

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    await server.complete_worker_task(task)
    await server.complete_worker_task(task)

    assert len(starts) == 2
    assert starts[0] == starts[1]
    assert starts[0][0] == (chat.id, "worker", task.id)
    assert starts[0][1]["turn_id"].startswith("turn_")


async def test_task_readiness_reports_queue_state(monkeypatch):
    task = server.worker.Task(
        id="task_1",
        chat_id="chat_1",
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
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
        id="task_1",
        chat_id="chat_1",
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        status="running",
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


async def _save_worker(worker_id: str, chat_id: str) -> None:
    await server.worker.store.save(
        server.worker.Worker(
            id=worker_id,
            chat_id=chat_id,
            sandbox_name=f"hatchery-{worker_id}",
            command_topic=f"hatchery-worker-{worker_id}-commands-v1",
            title="worker",
            status="running",
            spec=server.worker.WorkerSpec(),
            daemon_token="token",
            created_at="2026-08-31T00:00:00+00:00",
            updated_at="2026-08-31T00:00:00+00:00",
        )
    )


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
    await _save_worker(task.worker_id, task.chat_id)

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
    assert (
        assistant.data.attrs["braintrust.output_json"]
        == '{"text": "Finished reading."}'
    )
    assert completed.trace_id == parent.trace_id
    assert completed.parent_id == parent.id
    stored = await server.worker.store.get_task(task.id)
    assert stored is not None
    assert stored.telemetry_span["ended_at"] is not None
    assert (
        stored.telemetry_span["data"]["attrs"]["braintrust.output_json"]
        == '{"summary": "done"}'
    )
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
    await _save_worker(task.worker_id, task.chat_id)
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


async def test_worker_event_rejects_event_from_another_worker(monkeypatch):
    task = server.worker.Task(
        id="task_1",
        chat_id="chat_1",
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        created_at="2026-09-14T00:00:00+00:00",
        updated_at="2026-09-14T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    await _save_worker("wrk_1", task.chat_id)
    await _save_worker("wrk_2", "chat_2")

    async def ingest(event):
        raise AssertionError("wrong-worker event reached ingestion")

    monkeypatch.setattr(server.worker, "ingest", ingest)

    await server.worker_event(
        server.worker_protocol.Event(
            id="evt_forged",
            worker_id="wrk_2",
            task_id=task.id,
            sequence=0,
            type="task.completed",
            created_at="2026-09-14T00:00:01+00:00",
            payload={"summary": "forged"},
        )
    )

    assert await server.worker.store.get_task(task.id) == task


async def test_worker_event_rejects_old_daemon_whose_worker_is_unknown(monkeypatch):
    # A sandbox started before the cutover: its task row is still in the shared
    # task table, but its worker record is not in the new worker table.
    task = server.worker.Task(
        id="task_old",
        chat_id="chat_old",
        worker_id="wrk_old",
        title="fix",
        prompt="fix it",
        model="openai/test",
        created_at="2026-09-14T00:00:00+00:00",
        updated_at="2026-09-14T00:00:00+00:00",
    )
    await server.worker.store.save_task(task)
    started = []

    async def start_turn(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)

    await server.worker_event(
        server.worker_protocol.Event(
            id="evt_old",
            worker_id=task.worker_id,
            task_id=task.id,
            sequence=0,
            type="task.completed",
            created_at="2026-09-14T00:00:01+00:00",
            payload={"summary": "done"},
        )
    )

    assert await server.worker.store.get_task(task.id) == task
    assert started == []


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
        id="task_1",
        chat_id="chat_1",
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
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

    async def prepare(found):
        assert found is record

    monkeypatch.setattr(server.worker, "get_task", get_task)
    monkeypatch.setattr(server.worker.sandbox, "prepare_for_tty", prepare)
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
            id="task_1",
            chat_id="chat_1",
            worker_id="wrk_1",
            title="first",
            prompt="first task",
            model="openai/test",
            status="running",
            created_at="2026-08-31T00:00:00+00:00",
            updated_at="2026-08-31T00:00:01+00:00",
        ),
        "task_2": server.worker.Task(
            id="task_2",
            chat_id="chat_1",
            worker_id="wrk_1",
            title="second",
            prompt="second task",
            model="openai/test",
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

    async def prepare(found):
        assert found is record

    monkeypatch.setattr(server.worker, "get_task", get_task)
    monkeypatch.setattr(server.worker.sandbox, "prepare_for_tty", prepare)
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

    monkeypatch.setattr(
        server.worker.sandbox, "tty", lambda record: ("wss://tty.example", {})
    )
    monkeypatch.setattr(
        server.websockets.asyncio.client,
        "connect",
        lambda *args, **kwargs: Connection(),
    )
    ws = BridgeWebSocket()

    await server._bridge_tty(ws, type("Worker", (), {"id": "wrk_1"})(), "task_1")

    assert ws.closed == (4404, "session not found")


async def test_tty_bridge_maps_auth_rejection(monkeypatch):
    class Connection:
        async def __aenter__(self):
            raise websockets.InvalidStatus(Response(401, "Unauthorized", Headers()))

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        server.worker.sandbox, "tty", lambda record: ("wss://tty.example", {})
    )
    monkeypatch.setattr(
        server.websockets.asyncio.client,
        "connect",
        lambda *args, **kwargs: Connection(),
    )
    ws = BridgeWebSocket()

    await server._bridge_tty(ws, type("Worker", (), {"id": "wrk_1"})(), "task_1")

    assert ws.closed == (4401, "upstream rejected connection (401)")


async def test_tty_bridge_maps_connection_failure(monkeypatch):
    class Connection:
        async def __aenter__(self):
            raise OSError("unreachable")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        server.worker.sandbox, "tty", lambda record: ("wss://tty.example", {})
    )
    monkeypatch.setattr(
        server.websockets.asyncio.client,
        "connect",
        lambda *args, **kwargs: Connection(),
    )
    ws = BridgeWebSocket()

    await server._bridge_tty(ws, type("Worker", (), {"id": "wrk_1"})(), "task_1")

    assert ws.closed == (1011, "upstream connection failed")


async def test_channel_delivery_key_skips_completed_binding_on_retry():
    class Channel:
        name = "receipt-test"

        def __init__(self):
            self.delivered = []

        async def on_event(self, event, state):
            self.delivered.append((event.meta.id, event.data["message"], state))

    chat = await chats.create(None, "receipts")
    await chats.bind("receipt-test:thread", chat.id, "receipt-test", {"thread": "1"})
    channel = Channel()
    previous = server.bot.channels.get(channel.name)
    server.bot.channels[channel.name] = channel
    try:
        assert await server._deliver(
            chat.id, "done", delivery_key="turn_1:0"
        ) == []
        assert await server._deliver(
            chat.id, "done", delivery_key="turn_1:0"
        ) == []
    finally:
        if previous is None:
            del server.bot.channels[channel.name]
        else:
            server.bot.channels[channel.name] = previous

    assert len(channel.delivered) == 1
    assert channel.delivered[0][1:] == ("done", {"thread": "1"})
    assert len(await events.read(chat.id, "deliveries")) == 1


async def test_preview_http_does_not_activate_rotor(monkeypatch):
    activated = []

    async def activate(_worker):
        activated.append(True)

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_preview")
    monkeypatch.setenv("HATCHERY_PUBLIC_URL", "https://preview.example")
    monkeypatch.setattr(server.rotor_runtime.platform, "activate", activate)
    request = server.fastapi.Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/health",
            "headers": [(b"host", b"preview.example")],
            "scheme": "https",
            "server": ("preview.example", 443),
        }
    )

    await server._ensure_rotor_deployment(request)

    assert activated == []


async def test_production_canonical_request_activates_rotor(monkeypatch):
    activated = []
    store = server.rotor_runtime.worker.backends.store

    async def setup():
        return None

    async def active_deployment():
        return "dpl_old"

    async def activate(_worker):
        activated.append("dpl_new")

    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_new")
    monkeypatch.setenv("HATCHERY_PUBLIC_URL", "https://hatchery.example")
    monkeypatch.setattr(store, "setup", setup)
    monkeypatch.setattr(store, "active_deployment", active_deployment)
    monkeypatch.setattr(server.rotor_runtime.platform, "activate", activate)
    request = server.fastapi.Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/health",
            "headers": [(b"host", b"hatchery.example")],
            "scheme": "https",
            "server": ("hatchery.example", 443),
        }
    )

    await server._ensure_rotor_deployment(request)

    assert activated == ["dpl_new"]


async def test_explicit_rotor_activation_requires_release_secret(monkeypatch):
    activated = []

    async def activate(_worker):
        activated.append(True)

    monkeypatch.delenv("VERCEL_DEPLOYMENT_ID", raising=False)
    monkeypatch.setenv("ROTOR_RELEASE_SECRET", "release-secret")
    monkeypatch.setattr(server.rotor_runtime.platform, "activate", activate)
    async with client() as http:
        denied = await http.post("/api/rotor/activate")
        accepted = await http.post(
            "/api/rotor/activate",
            headers={"authorization": "Bearer release-secret"},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert activated == [True]


async def test_ui_retry_reuses_turn_identity(monkeypatch):
    agent = await server.agents.default()
    chat = await chats.create(agent.id, "retry")
    ui = ai.ui.ai_sdk.to_ui_messages([ai.user_message("once")])
    turns = []

    async def start_turn(
        chat_id, origin, task_id=None, turn_id=None, actor_user_id=None
    ):
        turns.append(turn_id)
        return server.turns.ActiveTurn(turn_id, "process_1", origin, task_id, 0)

    async def to_sse(_process_id, _turn_id):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(server.supervisor, "start_turn", start_turn)
    monkeypatch.setattr(server.agent_stream, "to_sse", to_sse)
    payload = {
        "chat_id": chat.id,
        "messages": [message.model_dump(mode="json") for message in ui],
    }
    async with client() as http:
        assert (await http.post("/api/chat", json=payload)).status_code == 200
        assert (await http.post("/api/chat", json=payload)).status_code == 200

    assert len(turns) == 2
    assert turns[0] == turns[1]


async def test_repository_and_thread_files_read_git_and_the_live_sandbox(run, repo):
    """Ported from agentmesh test_gateway repository and sandbox filesystem tests."""
    from hatchery.agent import tools
    from tests.agent import conftest

    call = ai.testing.tool_call
    script = [
        ai.user_message("Refine your persona"),
        ai.assistant_message(
            "Updating AGENTS.md.",
            call(tools.bash, command="edit-agents", description="edit persona"),
            call(tools.idle, note="persona refined"),
        ),
    ]
    commands = {"edit-agents": conftest.edit("self/AGENTS.md", "Be brief.\n")}
    async with run(script, commands=commands) as app:
        chat_id = await app.chat("Refine your persona")
        details = await app.details(chat_id)
        app.sandbox(details).inspection_files["repos/acme/untracked.log"] = b"not in Git\n"
        async with client() as c:
            main = await c.get("/api/repository")
            persona = await c.get(
                "/api/repository", params={"path": "agents/hatchery/AGENTS.md"}
            )
            edits = await c.get(
                "/api/repository",
                params={"chat_id": chat_id, "revision": details["checkpoint_sha"]},
            )
            diff = await c.get(
                "/api/repository",
                params={"chat_id": chat_id, "path": "agents/hatchery/AGENTS.md"},
            )
            proposal = await c.get(
                "/api/repository",
                params={"chat_id": chat_id, "proposal": details["proposals"][0]["branch"]},
            )
            rejected = [
                await c.get("/api/repository", params=params)
                for params in (
                    {"proposal": "main"},
                    {"comparison": "latest"},
                    {"comparison": "unknown"},
                    {"path": "../.env.local"},
                    {"chat_id": chat_id, "proposal": "consolidations/x/wiki/" + "0" * 64},
                )
            ]
            unknown_chat = await c.get("/api/repository", params={"chat_id": "chat_missing"})
            root = await c.get(f"/api/chats/{chat_id}/thread/filesystem")
            untracked = await c.get(
                f"/api/chats/{chat_id}/thread/filesystem/file",
                params={"path": "repos/acme/untracked.log"},
            )
            escape = await c.get(
                f"/api/chats/{chat_id}/thread/filesystem/file",
                params={"path": "../etc/passwd"},
            )
            tree = await c.get("/api/agents/hatchery/threads")
            detail = await c.get(f"/api/chats/{chat_id}/thread")
            transcript = await c.get(f"/api/chats/{chat_id}/transcript")

    assert main.status_code == 200
    assert "agents/hatchery/AGENTS.md" in [f["path"] for f in main.json()["files"]]
    assert main.json()["local_review"] is True
    # Ordinary workspace edits auto-merge at idle.
    assert persona.json()["after"]["text"] == "Be brief.\n"
    assert [(c["path"], c["status"]) for c in edits.json()["changes"]] == [
        ("agents/hatchery/AGENTS.md", "modified")
    ]
    assert diff.json()["before"]["text"] == "Be precise and preserve memory.\n"
    assert "+Be brief." in diff.json()["diff"]
    assert proposal.status_code == 200 and proposal.json()["merged"] is True
    assert [r.status_code for r in rejected] == [422, 422, 422, 422, 404]
    assert unknown_chat.status_code == 404
    assert {e["name"] for e in root.json()["entries"]} >= {"repos", "self"}
    assert untracked.json()["text"] == "not in Git\n"
    assert untracked.json()["notice"] is None
    assert escape.status_code == 422
    listed = tree.json()
    assert listed["local_review"] is True
    assert listed["threads"][0]["activity"]["phase"] == "idle"
    assert listed["threads"][0]["proposals"][0]["merged"] is True
    assert detail.json()["activity"]["mailbox_depth"] == 0
    # The console transcript keeps each step as its own message with its stored time.
    roles = [m["role"] for m in transcript.json()]
    assert roles[:3] == ["user", "assistant", "tool"]
    assistant = transcript.json()[1]
    assert isinstance(assistant["timestamp"], float) and assistant["turn"] == 1
    assert [p["kind"] for p in assistant["parts"]] == ["text", "tool_call", "tool_call"]
    assert "provider_metadata" not in assistant


# Operator API (ported from agentmesh tests/integration/test_gateway.py).


async def test_grant_is_durable_and_deduplicated(run):
    body = {"amount": 377, "request_id": "extra-allowance-9"}
    async with run() as app:
        async with client() as c:
            assert (await c.post("/api/agents/hatchery/grants", json=body)).status_code == 202
            assert (await c.post("/api/agents/hatchery/grants", json=body)).status_code == 202
            await app.rt.drain()
            assert (await c.post("/api/agents/hatchery/grants", json=body)).status_code == 202
            await app.rt.drain()
            budget = (await c.get("/api/agents/hatchery/threads")).json()["budget"]
            invalid = await c.post(
                "/api/agents/hatchery/grants", json={"amount": 0, "request_id": "b"}
            )

    assert budget["granted"] == 377
    assert budget["limit"] == 1000 + 377
    assert invalid.status_code == 422
