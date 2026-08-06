import fastapi
import fastapi.testclient

import chat
from chat import protocol


class FakeChannel:
    """Records delivered events; webhook handler dispatches the body as text."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.delivered: list[tuple[str, chat.Event]] = []

    async def handle(self, webhook: chat.Webhook, bus: chat.Bus) -> chat.Ack:
        inbound = chat.Inbound(token="t1", text=webhook.body.decode(), state={})
        return chat.Ack(work=bus.dispatch(inbound))

    async def on_event(self, event: chat.Event, sess: chat.Session) -> None:
        self.delivered.append((event.type, event))


async def test_dispatch_runs_handler_and_persists_history():
    async def handler(turn: chat.Turn) -> None:
        await turn.reply(f"echo: {turn.message.content}")

    app = chat.App(handler)
    fake = FakeChannel()
    app.add(fake)
    sess = await app._dispatch(fake, chat.Inbound(token="t1", text="hi", state={"k": "v"}))

    assert [(m.role, m.content) for m in sess.history] == [("user", "hi"), ("assistant", "echo: hi")]
    assert [t for t, _ in fake.delivered] == ["turn.started", "message.completed", "turn.completed"]
    stored = await app.store.get(sess.id)
    assert stored is not None
    assert len(stored.history) == 2


async def test_dispatch_same_token_continues_session():
    async def handler(turn: chat.Turn) -> None:
        await turn.reply("ok")

    app = chat.App(handler)
    fake = FakeChannel()
    first = await app._dispatch(fake, chat.Inbound(token="t1", text="one", state={}))
    second = await app._dispatch(fake, chat.Inbound(token="t1", text="two", state={}))
    assert first.id == second.id
    assert len(second.history) == 4  # both turns visible to the handler


async def test_handler_failure_emits_turn_failed():
    async def handler(turn: chat.Turn) -> None:
        raise RuntimeError("boom")

    app = chat.App(handler)
    fake = FakeChannel()
    await app._dispatch(fake, chat.Inbound(token="t1", text="hi", state={}))
    types = [t for t, _ in fake.delivered]
    assert types == ["turn.started", "turn.failed"]
    [failed] = [ev for t, ev in fake.delivered if t == protocol.TURN_FAILED]
    assert failed.data["error"] == "boom"


async def test_channel_delivery_failure_does_not_kill_turn():
    class BrokenChannel(FakeChannel):
        async def on_event(self, event: chat.Event, sess: chat.Session) -> None:
            raise RuntimeError("slack is down")

    replies: list[str] = []

    async def handler(turn: chat.Turn) -> None:
        replies.append(turn.message.content)
        await turn.reply("ok")

    app = chat.App(handler)
    sess = await app._dispatch(BrokenChannel(), chat.Inbound(token="t1", text="hi", state={}))
    assert replies == ["hi"]
    assert len(sess.history) == 2


def test_router_dispatches_webhooks_and_404s_unknown_channels():
    async def handler(turn: chat.Turn) -> None:
        await turn.reply("done")

    bot = chat.App(handler)
    fake = FakeChannel()
    bot.add(fake)
    server = fastapi.FastAPI()
    server.include_router(bot.router)
    client = fastapi.testclient.TestClient(server)

    response = client.post("/chat/v1/fake", content=b"hello")
    assert response.status_code == 200
    # TestClient runs background work before returning
    assert [t for t, _ in fake.delivered] == ["turn.started", "message.completed", "turn.completed"]

    assert client.post("/chat/v1/nope", content=b"x").status_code == 404
