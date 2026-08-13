import fastapi
import fastapi.testclient

import channels
from store import chats, events, projects


class FakeChannel:
    """Records delivered events; webhook handler dispatches the body as text."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.delivered: list[tuple[str, dict]] = []

    async def handle(self, webhook: channels.Webhook, bus: channels.Bus) -> channels.Ack:
        inbound = channels.Inbound(token="t1", text=webhook.body.decode(), state={}, title="fake chat")
        return channels.Ack(work=bus.dispatch(inbound))

    async def on_event(self, event: channels.Event, state: dict) -> None:
        self.delivered.append((event.type, state))


def make_app(started: list) -> tuple[channels.App, FakeChannel]:
    async def start_turn(chat: chats.Chat, text: str) -> None:
        started.append((chat.id, text))

    app = channels.App(start_turn)
    fake = FakeChannel()
    app.add(fake)
    return app, fake


async def test_dispatch_claims_chat_appends_message_and_starts_turn():
    started: list = []
    app, fake = make_app(started)
    inbound = channels.Inbound(token="t1", text="hi", state={"k": "v"}, title="hello thread")
    chat = await app._dispatch(fake, inbound)

    assert chat.title == "hello thread"
    project = await projects.get(chat.project_id)
    assert project is not None and project.name == projects.DEFAULT_NAME
    records = await events.read(chat.id)
    assert [data["type"] for _, data in records] == ["message.received"]
    assert records[0][1]["data"] == {"message": "hi", "channel": "fake"}
    assert started == [(chat.id, "hi")]
    # the append fanned out to the chat's binding with its state
    assert fake.delivered == [("message.received", {"k": "v"})]


async def test_same_token_continues_chat():
    started: list = []
    app, fake = make_app(started)
    first = await app._dispatch(fake, channels.Inbound(token="t1", text="one", state={}))
    second = await app._dispatch(fake, channels.Inbound(token="t1", text="two", state={}))
    assert first.id == second.id
    assert len(await events.read(first.id)) == 2


async def test_repo_routes_to_owning_project():
    project = await projects.create("sdk")
    await projects.set_repos(project.id, ["vercel/repo"])
    started: list = []
    app, fake = make_app(started)
    inbound = channels.Inbound(token="t2", text="hi", state={}, repo="vercel/repo")
    chat = await app._dispatch(fake, inbound)
    assert chat.project_id == project.id


async def test_start_turn_failure_emits_turn_failed():
    async def start_turn(chat: chats.Chat, text: str) -> None:
        raise RuntimeError("queue is down")

    app = channels.App(start_turn)
    fake = FakeChannel()
    app.add(fake)
    chat = await app._dispatch(fake, channels.Inbound(token="t1", text="hi", state={}))
    types = [data["type"] for _, data in await events.read(chat.id)]
    assert types == ["message.received", "turn.failed"]
    assert ("turn.failed", {}) in fake.delivered


async def test_delivery_failure_does_not_kill_dispatch():
    class BrokenChannel(FakeChannel):
        async def on_event(self, event: channels.Event, state: dict) -> None:
            raise RuntimeError("slack is down")

    started: list = []

    async def start_turn(chat: chats.Chat, text: str) -> None:
        started.append(chat.id)

    app = channels.App(start_turn)
    broken = BrokenChannel()
    app.add(broken)
    chat = await app._dispatch(broken, channels.Inbound(token="t1", text="hi", state={}))
    assert started == [chat.id]
    assert len(await events.read(chat.id)) == 1


def test_router_dispatches_webhooks_and_404s_unknown_channels():
    started: list = []
    app, fake = make_app(started)
    server = fastapi.FastAPI()
    server.include_router(app.router)
    client = fastapi.testclient.TestClient(server)

    response = client.post("/channels/v1/fake", content=b"hello")
    assert response.status_code == 200
    # TestClient runs background work before returning
    assert len(started) == 1

    assert client.post("/channels/v1/nope", content=b"x").status_code == 404
