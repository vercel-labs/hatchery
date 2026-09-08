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
        self.bindings: dict[str, dict] = {}
        self.lookups: list[str] = []

    async def binding(self, token: str) -> dict | None:
        self.lookups.append(token)
        return self.bindings.get(token)

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
    assert inbound.state == {
        "channel_id": "C1", "thread_ts": "100.1", "team_id": "T1", "user_id": "U1",
        "message_id": "100.1", "display_text": "<@UBOT> hello", "author": "U1",
    }
    assert bus.lookups == ["T1:C1:100.1"]


async def test_thread_reply_reuses_thread_root_token():
    calls: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "messages": [mention(thread_ts="50.0", ts="100.2")]})

    bus = FakeBus()
    channel = slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))
    ack = await channel.handle(forwarded(envelope(mention(thread_ts="50.0", ts="100.2"))), bus)
    assert ack.work is not None
    await ack.work
    assert bus.dispatched[0].token == "C1:50.0"


async def test_direct_message_dispatches():
    dm = {"type": "message", "channel_type": "im", "channel": "D1", "ts": "1.1", "user": "U1", "text": "hi"}
    _, bus = await handled(forwarded(envelope(dm)))
    assert bus.dispatched[0].token == "D1:1.1"


async def test_untagged_thread_reply_dispatches_when_classifier_selects_it(monkeypatch):
    calls: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "UBOT", "text": "Should I open the pull request?", "ts": "1.1"},
                    {"user": "U1", "text": "yes, please do", "ts": "1.2"},
                ],
            },
        )

    channel = slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))
    seen = {}

    async def should_invoke(transcript, newest_ts, bot_user_ids):
        seen["classification"] = (transcript, newest_ts, bot_user_ids)
        return True

    monkeypatch.setattr(channel, "_should_invoke", should_invoke)
    bus = FakeBus()
    reply = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "thread_ts": "1.0",
        "ts": "1.2",
        "user": "U1",
        "text": "yes, please do",
    }
    ack = await channel.handle(forwarded(envelope(reply)), bus)
    assert ack.work is not None
    await ack.work

    assert [request.url.path for request in calls] == ["/api/conversations.replies"]
    params = dict(urllib.parse.parse_qsl(calls[0].read().decode()))
    assert params == {"channel": "C1", "ts": "1.0", "limit": "100"}
    assert seen["classification"] == (
        [
            {
                "sender": "UBOT",
                "text": "Should I open the pull request?",
                "ts": "1.1",
                "newest": False,
            },
            {"sender": "U1", "text": "yes, please do", "ts": "1.2", "newest": True},
        ],
        "1.2",
        ["UBOT"],
    )
    assert len(bus.dispatched) == 2
    stored, wake = bus.dispatched
    assert stored.token == "C1:1.0"
    assert stored.invoke is False
    assert "yes, please do" in stored.text
    assert wake.persist is False
    assert wake.invoke is True


async def test_untagged_thread_reply_is_stored_when_classifier_rejects_it(monkeypatch):
    channel = api_channel([])

    async def replies(method, **params):
        return {"ok": True, "messages": [mention(ts="1.0"), {"user": "U1", "text": "thanks", "ts": "1.2"}]}

    async def should_invoke(transcript, newest_ts, bot_user_ids):
        return False

    monkeypatch.setattr(channel, "_api", replies)
    monkeypatch.setattr(channel, "_should_invoke", should_invoke)
    bus = FakeBus()
    reply = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "thread_ts": "1.0",
        "ts": "1.2",
        "user": "U1",
        "text": "thanks",
    }
    ack = await channel.handle(forwarded(envelope(reply)), bus)
    assert ack.work is not None
    await ack.work
    root, stored = bus.dispatched
    assert root.invoke is False
    assert stored.invoke is False
    assert "thanks" in stored.text


async def test_classifier_failure_still_stores_thread_reply(monkeypatch):
    channel = api_channel([])

    async def replies(method, **params):
        return {
            "ok": True,
            "messages": [
                mention(ts="1.0"),
                {"user": "U1", "text": "are you receiving this?", "ts": "1.2"},
            ],
        }

    async def should_invoke(transcript, newest_ts, bot_user_ids):
        raise ValueError("empty structured output")

    monkeypatch.setattr(channel, "_api", replies)
    monkeypatch.setattr(channel, "_should_invoke", should_invoke)
    bus = FakeBus()
    reply = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "thread_ts": "1.0",
        "ts": "1.2",
        "user": "U1",
        "text": "are you receiving this?",
    }
    ack = await channel.handle(forwarded(envelope(reply)), bus)
    assert ack.work is not None
    await ack.work
    root, stored = bus.dispatched
    assert root.invoke is False
    assert stored.invoke is False
    assert "are you receiving this?" in stored.text


async def test_ignores_plain_channel_message_bots_and_self():
    plain = {"type": "message", "channel_type": "channel", "channel": "C1", "ts": "1.1", "user": "U1", "text": "hi"}
    duplicate_mention = {"type": "message", "channel_type": "channel", "channel": "C1", "thread_ts": "1.0", "ts": "1.1", "user": "U1", "text": "<@UBOT> hi"}
    bot_reply = {"type": "message", "channel_type": "channel", "channel": "C1", "thread_ts": "1.0", "ts": "1.1", "user": "U2", "bot_id": "B99", "text": "hi"}
    self_reply = {"type": "message", "channel_type": "channel", "channel": "C1", "thread_ts": "1.0", "ts": "1.1", "user": "UBOT", "text": "hi"}
    dm_subtype = {"type": "message", "channel_type": "im", "channel": "D1", "ts": "1.1", "subtype": "channel_join"}
    from_bot = mention(bot_id="B99")
    from_self = mention(user="UBOT")
    for event in (plain, duplicate_mention, bot_reply, self_reply, dm_subtype, from_bot, from_self):
        _, bus = await handled(forwarded(envelope(event)))
        assert bus.dispatched == [], event


async def test_retries_retain_source_id_for_server_dedupe():
    bus = FakeBus()
    await handled(forwarded(envelope(mention(), event_id="Ev1")), bus)
    await handled(forwarded(envelope(mention(), event_id="Ev1")), bus)
    await handled(forwarded(envelope(mention(), event_id="Ev2")), bus)
    assert [inbound.state["message_id"] for inbound in bus.dispatched] == ["100.1"] * 3
    assert bus.seen == set()


def api_channel(calls: list) -> slack.SlackChannel:
    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/conversations.replies":
            return httpx.Response(
                200,
                json={"ok": True, "messages": [{"user": "U1", "text": "thanks", "ts": "1.2"}]},
            )
        return httpx.Response(200, json={"ok": True})

    return slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))


def state() -> dict:
    return {"channel_id": "C1", "thread_ts": "100.1"}


async def test_space_assignment_updates_status():
    calls: list[httpx.Request] = []
    channel = api_channel(calls)
    await channel.on_event(channels.event(channels.protocol.SPACE_ASSIGNING), state())
    await channel.on_event(
        channels.event(
            channels.protocol.SPACE_ASSIGNED,
            space={"id": "spc_docs", "name": "docs"},
        ),
        state(),
    )
    statuses = [
        dict(urllib.parse.parse_qsl(request.read().decode()))["status"]
        for request in calls
    ]
    assert statuses == ["assigning a space...", "assigned docs"]


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


async def test_intermediate_reply_becomes_opaque_status():
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(
        channels.event(channels.protocol.MESSAGE_COMPLETED, message="I will inspect that.", final=False),
        state(),
    )
    [request] = calls
    assert request.url.path == "/api/assistant.threads.setStatus"
    params = dict(urllib.parse.parse_qsl(request.read().decode()))
    assert params == {"channel_id": "C1", "thread_ts": "100.1", "status": "is working..."}


@pytest.mark.parametrize("origin,source", [("ui", "Hatchery UI"), ("slack", "Slack"), ("github", "GitHub")])
async def test_human_message_uses_event_author_not_binding_identity(origin, source):
    calls: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "200.1"})

    channel = slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))
    slack_state = {**state(), "team_id": "T1", "user_id": "someone-else", "author": "Wrong Name"}
    event = channels.event(channels.protocol.MESSAGE_RECEIVED, message="continue here", origin=origin, author="Andrey")
    response = await channel.on_event(event, slack_state)

    assert response == {"ok": True, "ts": "200.1"}
    assert [request.url.path for request in calls] == ["/api/chat.postMessage"]
    params = dict(urllib.parse.parse_qsl(calls[0].read().decode()))
    assert params == {
        "channel": "C1", "thread_ts": "100.1",
        "text": f"Andrey · via {source}\n\ncontinue here",
    }


@pytest.mark.parametrize("bound", [True, False])
async def test_bound_thread_syncs_all_pages_without_bot_subscription(monkeypatch, bound):
    calls: list[httpx.Request] = []
    reply = {
        "type": "message", "channel_type": "channel", "channel": "C1",
        "thread_ts": "1.0", "ts": "1.4", "user": "U2", "text": "thanks &amp; noted",
    }

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        params = dict(urllib.parse.parse_qsl(request.read().decode()))
        if "cursor" not in params:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [
                    {"user": "UOTHERBOT", "bot_id": "B1", "text": "shared root", "ts": "1.0"},
                    {"user": "U1", "text": "hello", "ts": "1.1"},
                ],
                "response_metadata": {"next_cursor": "page2"},
            })
        assert params["cursor"] == "page2"
        return httpx.Response(200, json={
            "ok": True,
            "messages": [
                {"user": "UOTHERBOT", "subtype": "bot_message", "text": "bot", "ts": "1.2"},
                {"user": "U1", "subtype": "channel_join", "text": "joined", "ts": "1.3"},
                reply,
            ],
            "response_metadata": {"next_cursor": ""},
        })

    channel = slack.channel(connector="slack/e2e-bot", transport=httpx.MockTransport(responder))

    async def should_invoke(*args):
        return False

    monkeypatch.setattr(channel, "_should_invoke", should_invoke)
    bus = FakeBus()
    if bound:
        bus.bindings["T1:C1:1.0"] = {}  # empty state is still an existing binding
    else:
        bus.bindings["T2:C1:1.0"] = {}  # another team must not grant access
    ack = await channel.handle(forwarded(envelope(reply)), bus)
    assert ack.work is not None
    await ack.work
    assert bus.lookups == ["T1:C1:1.0"]
    assert len(calls) == 2
    if not bound:
        assert bus.dispatched == []
        return
    assert [message.state["message_id"] for message in bus.dispatched] == ["1.1", "1.4"]
    assert all(message.token == "C1:1.0" and message.persist and not message.invoke for message in bus.dispatched)
    assert bus.dispatched[-1].state["display_text"] == "thanks & noted"
    assert bus.dispatched[-1].state["author"] == "U2"


async def test_failed_persistence_does_not_burn_retry():
    class FailingBus(FakeBus):
        async def dispatch(self, inbound):
            if not self.dispatched:
                self.dispatched.append(inbound)
                raise RuntimeError("store unavailable")
            await super().dispatch(inbound)

    bus = FailingBus()
    webhook = forwarded(envelope(mention()))
    with pytest.raises(RuntimeError, match="store unavailable"):
        await handled(webhook, bus)
    await handled(webhook, bus)
    assert len(bus.dispatched) == 2
    assert bus.dispatched[0].state["message_id"] == bus.dispatched[1].state["message_id"]
    assert not bus.seen


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
