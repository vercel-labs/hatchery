import json
import urllib.parse

import httpx
import pytest

import channels
from channels import slack


@pytest.fixture(autouse=True)
def connect_stub(monkeypatch):
    """Stand in for vercel connect: OIDC verification + token minting."""
    minted: list[str] = []

    async def verify(headers):
        if headers.get("authorization") != "Bearer good":
            raise slack.connect.ConnectWebhookVerificationError("unverified")

    async def get_token(connector, *, subject, **kwargs):
        minted.append(connector)
        return "xoxb-connect"

    monkeypatch.setattr(slack.connect, "verify_connect_webhook", verify)
    monkeypatch.setattr(slack.connect, "get_token", get_token)
    return minted


class FakeBus:
    def __init__(self) -> None:
        self.dispatched: list[channels.Inbound] = []
        self.seen: set[str] = set()

    async def dispatch(self, inbound: channels.Inbound) -> None:
        self.dispatched.append(inbound)

    async def dedupe(self, key: str) -> bool:
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def forwarded(payload: dict, auth: str = "Bearer good", extra_headers: dict | None = None) -> channels.Webhook:
    headers = {"authorization": auth}
    headers.update(extra_headers or {})
    return channels.Webhook(body=json.dumps(payload).encode(), headers=headers)


def envelope(event: dict, event_id: str = "Ev1") -> dict:
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": event_id,
        "authorizations": [{"user_id": "UBOT"}],
        "event": event,
    }


def mention(text: str = "<@UBOT> hello", **overrides) -> dict:
    event = {"type": "app_mention", "channel": "C1", "ts": "100.1", "user": "U1", "text": text}
    event.update(overrides)
    return event


async def handled(webhook: channels.Webhook, bus: FakeBus | None = None) -> tuple[channels.Ack, FakeBus]:
    bus = bus or FakeBus()
    ack = await slack.channel(connector="slack/e2e-bot").handle(webhook, bus)
    if ack.work is not None:
        await ack.work
    return ack, bus


async def test_rejects_unverified_forward():
    ack, bus = await handled(forwarded(envelope(mention()), auth="Bearer forged"))
    assert ack.status == 401
    assert bus.dispatched == []


async def test_url_verification_challenge():
    ack, _ = await handled(forwarded({"type": "url_verification", "challenge": "ch4llenge"}))
    assert (ack.status, ack.body, ack.content_type) == (200, "ch4llenge", "text/plain")


async def test_drops_http_timeout_retries():
    webhook = forwarded(
        envelope(mention()), extra_headers={"x-slack-retry-num": "1", "x-slack-retry-reason": "http_timeout"}
    )
    ack, bus = await handled(webhook)
    assert ack.status == 200
    assert bus.dispatched == []


async def test_app_mention_dispatches_with_thread_token_and_attribution():
    _, bus = await handled(forwarded(envelope(mention())))
    [inbound] = bus.dispatched
    assert inbound.token == "C1:100.1"  # thread_ts falls back to ts
    assert "<@UBOT> hello" in inbound.text
    assert '<slack_message channel="C1"' in inbound.text
    assert 'sender="U1"' in inbound.text
    assert inbound.title == "slack: hello"
    assert inbound.state == {"channel_id": "C1", "thread_ts": "100.1", "team_id": "T1", "user_id": "U1"}


async def test_thread_reply_reuses_thread_root_token():
    _, bus = await handled(forwarded(envelope(mention(thread_ts="50.0", ts="100.2"))))
    assert bus.dispatched[0].token == "C1:50.0"


async def test_direct_message_dispatches():
    dm = {"type": "message", "channel_type": "im", "channel": "D1", "ts": "1.1", "user": "U1", "text": "hi"}
    _, bus = await handled(forwarded(envelope(dm)))
    assert bus.dispatched[0].token == "D1:1.1"


async def test_ignores_plain_channel_message_bots_and_self():
    plain = {"type": "message", "channel_type": "channel", "channel": "C1", "ts": "1.1", "user": "U1", "text": "hi"}
    dm_subtype = {"type": "message", "channel_type": "im", "channel": "D1", "ts": "1.1", "subtype": "channel_join"}
    from_bot = mention(bot_id="B99")
    from_self = mention(user="UBOT")
    for event in (plain, dm_subtype, from_bot, from_self):
        _, bus = await handled(forwarded(envelope(event)))
        assert bus.dispatched == [], event


async def test_dedupes_event_id():
    bus = FakeBus()
    await handled(forwarded(envelope(mention(), event_id="Ev1")), bus)
    await handled(forwarded(envelope(mention(), event_id="Ev1")), bus)
    await handled(forwarded(envelope(mention(), event_id="Ev2")), bus)
    assert len(bus.dispatched) == 2


def api_channel(calls: list) -> slack.SlackChannel:
    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    return slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))


def state() -> dict:
    return {"channel_id": "C1", "thread_ts": "100.1"}


async def test_turn_started_sets_typing_status_with_connect_token(connect_stub):
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.TURN_STARTED), state())
    [request] = calls
    assert request.url.path == "/api/assistant.threads.setStatus"
    params = dict(urllib.parse.parse_qsl(request.read().decode()))
    assert params == {"channel_id": "C1", "thread_ts": "100.1", "status": "is thinking..."}
    assert request.headers["authorization"] == "Bearer xoxb-connect"
    assert connect_stub == ["slack/e2e-bot"]  # token minted from the connector


async def test_reply_posts_into_thread():
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.MESSAGE_COMPLETED, message="done!"), state())
    [request] = calls
    assert request.url.path == "/api/chat.postMessage"
    params = dict(urllib.parse.parse_qsl(request.read().decode()))
    assert params == {"channel": "C1", "thread_ts": "100.1", "text": "done!"}


async def test_ui_message_uses_slack_user_profile_with_ui_attribution():
    calls: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/users.info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "user": {"profile": {"display_name": "Andrey", "image_72": "https://img/andrey.png"}},
                },
            )
        return httpx.Response(200, json={"ok": True})

    channel = slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))
    slack_state = {**state(), "team_id": "T1", "user_id": "U1"}
    event = channels.event(channels.protocol.MESSAGE_RECEIVED, message="continue here", origin="ui")
    await channel.on_event(event, slack_state)
    await channel.on_event(event, slack_state)

    assert [request.url.path for request in calls] == [
        "/api/users.info",
        "/api/chat.postMessage",
        "/api/chat.postMessage",
    ]
    params = dict(urllib.parse.parse_qsl(calls[1].read().decode()))
    assert params == {
        "channel": "C1",
        "thread_ts": "100.1",
        "text": "continue here",
        "username": "Andrey · via Hatchery UI",
        "icon_url": "https://img/andrey.png",
    }


async def test_status_is_truncated():
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.STATUS_UPDATED, status="x" * 80), state())
    params = dict(urllib.parse.parse_qsl(calls[0].read().decode()))
    assert len(params["status"]) == slack.STATUS_LIMIT


async def test_turn_failed_posts_error():
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.TURN_FAILED, error="boom"), state())
    params = dict(urllib.parse.parse_qsl(calls[0].read().decode()))
    assert params["text"] == "something went wrong: boom"


async def test_api_error_raises():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    ch = slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))
    with pytest.raises(RuntimeError, match="channel_not_found"):
        await ch.on_event(channels.event(channels.protocol.MESSAGE_COMPLETED, message="hi"), state())
