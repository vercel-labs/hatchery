import fastapi
import fastapi.testclient

import channels


class FakeHub:
    def __init__(self) -> None:
        self.dispatched: list[tuple[str, channels.Inbound]] = []
        self.deduped: list[str] = []

    async def dispatch(self, channel: str, inbound: channels.Inbound) -> None:
        self.dispatched.append((channel, inbound))

    async def dedupe(self, key: str) -> bool:
        self.deduped.append(key)
        return True


class FakeChannel:
    """Webhook handler dedupes on a fixed key and dispatches the body as text."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    async def handle(self, webhook: channels.Webhook, bus: channels.Bus) -> channels.Ack:
        await bus.dedupe("d1")
        inbound = channels.Inbound(token="t1", text=webhook.body.decode(), state={}, title="fake chat")
        return channels.Ack(work=bus.dispatch(inbound))

    async def on_event(self, event: channels.Event, state: dict) -> None:
        pass


def make_client() -> tuple[fastapi.testclient.TestClient, FakeHub]:
    hub = FakeHub()
    app = channels.App(hub)
    app.add(FakeChannel())
    server = fastapi.FastAPI()
    server.include_router(app.router)
    return fastapi.testclient.TestClient(server), hub


def test_router_dispatches_webhooks_to_hub():
    client, hub = make_client()
    response = client.post("/channels/v1/fake", content=b"hello")
    assert response.status_code == 200
    # TestClient runs background work before returning
    [(channel, inbound)] = hub.dispatched
    assert channel == "fake"
    assert inbound.text == "hello"


def test_dedupe_keys_are_channel_scoped():
    client, hub = make_client()
    client.post("/channels/v1/fake", content=b"x")
    assert hub.deduped == ["fake:d1"]


def test_unknown_channel_404s():
    client, _ = make_client()
    assert client.post("/channels/v1/nope", content=b"x").status_code == 404
