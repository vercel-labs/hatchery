from unittest import mock

import fastapi.testclient

from app import server


def client(monkeypatch, started: list) -> fastapi.testclient.TestClient:
    async def start_turn(chat, text: str) -> None:
        started.append((chat.id, text))

    monkeypatch.setattr(server.bot, "start_turn", start_turn)
    return fastapi.testclient.TestClient(server.app)


def test_health(monkeypatch):
    with client(monkeypatch, []) as c:
        body = c.get("/api/health").json()
        assert body["ok"] is True
        assert set(body["channels"]) == {"slack", "github"}


def test_project_and_chat_flow(monkeypatch):
    started: list = []
    with client(monkeypatch, started) as c:
        assert "default" in [p["name"] for p in c.get("/api/projects").json()]  # lifespan seeds it

        project = c.post("/api/projects", json={"name": "sdk"}).json()
        c.put(f"/api/projects/{project['id']}/memory", json={"memory": "port the tests"})
        c.put(f"/api/projects/{project['id']}/repos", json={"repos": ["vercel/workflow"]})
        loaded = c.get(f"/api/projects/{project['id']}").json()
        assert loaded["memory"] == "port the tests"
        assert loaded["repos"] == ["vercel/workflow"]
        assert loaded["chats"] == []

        chat = c.post("/api/chats", json={"project_id": project["id"], "title": "hello"}).json()
        assert c.post(f"/api/chats/{chat['id']}/messages", json={"message": "hi"}).status_code == 200
        assert started == [(chat["id"], "hi")]

        loaded = c.get(f"/api/chats/{chat['id']}").json()
        assert [e["type"] for e in loaded["events"]] == ["message.received"]
        assert loaded["events"][0]["data"] == {"message": "hi", "channel": "ui"}

        archived = c.post(f"/api/chats/{chat['id']}/status", json={"status": "archived"}).json()
        assert archived["status"] == "archived"


def test_unknown_ids_404(monkeypatch):
    with client(monkeypatch, []) as c:
        assert c.get("/api/projects/prj_missing").status_code == 404
        assert c.get("/api/chats/cht_missing").status_code == 404
        assert c.post("/api/chats/cht_missing/messages", json={"message": "x"}).status_code == 404
        assert c.get("/api/chats/cht_missing/stream").status_code == 404
        assert c.post("/api/chats", json={"project_id": "prj_missing"}).status_code == 404


def test_cron_parity_creates_stable_chat_and_starts_workflow(monkeypatch):
    from agent.tasks import parity

    started = mock.AsyncMock()
    monkeypatch.setattr(server.vercel.workflow, "start", started)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with client(monkeypatch, []) as c:
        first = c.get("/api/cron/parity").json()
        second = c.get("/api/cron/parity").json()
        assert first["chat_id"] == second["chat_id"]  # one stable daily chat
        assert started.await_count == 2
        assert started.await_args.args == (parity.parity_workflow, first["chat_id"])
        events = c.get(f"/api/chats/{first['chat_id']}").json()["events"]
        assert [e["type"] for e in events] == ["message.received", "message.received"]


def test_cron_parity_requires_secret_when_set(monkeypatch):
    monkeypatch.setattr(server.vercel.workflow, "start", mock.AsyncMock())
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    with client(monkeypatch, []) as c:
        assert c.get("/api/cron/parity").status_code == 401
        assert c.get("/api/cron/parity", headers={"authorization": "Bearer s3cret"}).status_code == 200


def test_stream_tails_events(monkeypatch):
    from app import api

    monkeypatch.setattr(api, "STREAM_LIMIT_SECONDS", 1)  # TestClient drains the tail on close
    started: list = []
    with client(monkeypatch, started) as c:
        project = c.post("/api/projects", json={"name": "sdk"}).json()
        chat = c.post("/api/chats", json={"project_id": project["id"]}).json()
        c.post(f"/api/chats/{chat['id']}/messages", json={"message": "hi"})
        with c.stream("GET", f"/api/chats/{chat['id']}/stream") as response:
            line = next(response.iter_lines())
        assert '"message.received"' in line
