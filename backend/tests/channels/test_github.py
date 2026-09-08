import json

import httpx
import pytest

import channels
from channels import github


@pytest.fixture(autouse=True)
def connect_stub(monkeypatch):
    """Stand in for vercel connect: OIDC verification + token minting."""
    minted: list[str] = []

    async def verify(headers):
        if headers.get("authorization") != "Bearer good":
            raise github.connect.ConnectWebhookVerificationError("unverified")

    async def get_token(connector, *, subject, **kwargs):
        minted.append(connector)
        return "ghs_connect"

    monkeypatch.setattr(github.connect, "verify_connect_webhook", verify)
    monkeypatch.setattr(github.connect, "get_token", get_token)
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


def forwarded(payload: dict, event: str, delivery: str = "d1", auth: str = "Bearer good") -> channels.Webhook:
    return channels.Webhook(
        body=json.dumps(payload).encode(),
        headers={"authorization": auth, "x-github-event": event, "x-github-delivery": delivery},
    )


def issue_comment(body: str = "@e2e-bot please port this", pull: bool = False, **overrides) -> dict:
    issue: dict = {"number": 5}
    if pull:
        issue["pull_request"] = {"url": "..."}
    payload = {
        "action": "created",
        "comment": {"id": 900, "body": body, "html_url": "https://github.com/v/r/issues/5#issuecomment-900"},
        "issue": issue,
        "repository": {"id": 42, "full_name": "vercel/repo"},
        "sender": {"id": 7, "login": "andrey", "type": "User"},
    }
    payload.update(overrides)
    return payload


async def handled(webhook: channels.Webhook, bus: FakeBus | None = None) -> tuple[channels.Ack, FakeBus]:
    bus = bus or FakeBus()
    ack = await github.channel(connector="github/e2e-bot", bot_name="e2e-bot").handle(webhook, bus)
    if ack.work is not None:
        await ack.work
    return ack, bus


async def test_rejects_unverified_forward():
    ack, bus = await handled(forwarded(issue_comment(), "issue_comment", auth="Bearer forged"))
    assert ack.status == 401
    assert bus.dispatched == []


async def test_ping_ok():
    ack, _ = await handled(forwarded({"zen": "design for failure"}, "ping"))
    assert ack.status == 200


async def test_issue_comment_mention_dispatches():
    _, bus = await handled(forwarded(issue_comment(), "issue_comment"))
    [inbound] = bus.dispatched
    assert inbound.token == "repo:42:issue:5"
    assert "please port this" in inbound.text
    assert "@e2e-bot" not in inbound.text  # mention stripped
    assert 'repository="vercel/repo"' in inbound.text
    assert inbound.state["kind"] == "issue"
    assert inbound.state["number"] == 5
    assert inbound.state["sender_id"] == "7"
    assert inbound.state["message_id"] == 900
    assert inbound.state["display_text"] == "please port this"
    assert inbound.state["author"] == "andrey"
    assert inbound.persist and inbound.invoke


@pytest.mark.parametrize("pull", [False, True])
async def test_bound_unmentioned_comment_persists_without_invoking(pull):
    bus = FakeBus()
    token = f"repo:42:{'pull' if pull else 'issue'}:5"
    bus.bindings[token] = {}
    _, bus = await handled(forwarded(issue_comment(body="  progress update  ", pull=pull), "issue_comment"), bus)
    [inbound] = bus.dispatched
    assert bus.lookups == [token]
    assert inbound.token == token
    assert inbound.persist is True
    assert inbound.invoke is False
    assert inbound.state["message_id"] == 900
    assert inbound.state["display_text"] == "progress update"
    assert inbound.state["author"] == "andrey"


async def test_unbound_unmentioned_comment_is_ignored():
    ack, bus = await handled(forwarded(issue_comment(body="progress update"), "issue_comment"))
    assert ack.work is None
    assert bus.lookups == ["repo:42:issue:5"]
    assert bus.dispatched == []


async def test_bound_unmentioned_review_reply_keeps_root():
    payload = issue_comment()
    payload["comment"] = {"id": 901, "in_reply_to_id": 800, "body": "updated"}
    payload["pull_request"] = {"number": 9}
    bus = FakeBus()
    bus.bindings["repo:42:pull:9:review-comment:800"] = {}
    _, bus = await handled(forwarded(payload, "pull_request_review_comment"), bus)
    [inbound] = bus.dispatched
    assert inbound.token == "repo:42:pull:9:review-comment:800"
    assert inbound.state["root_comment_id"] == 800
    assert inbound.state["message_id"] == 901
    assert inbound.persist and not inbound.invoke


async def test_bound_thread_still_rejects_bots_and_mirror_marker():
    for payload in (
        issue_comment(body="automated", sender={"login": "robot[bot]", "type": "Bot"}),
        issue_comment(body=f"mirrored\n\n{github.MARKER}"),
        issue_comment(comment={"id": 900, "body": "automated", "user": {"login": "robot[bot]", "type": "Bot"}}),
    ):
        bus = FakeBus()
        bus.bindings["repo:42:issue:5"] = {}
        await handled(forwarded(payload, "issue_comment"), bus)
        assert bus.dispatched == []


async def test_pr_comment_gets_pull_token():
    _, bus = await handled(forwarded(issue_comment(pull=True), "issue_comment"))
    assert bus.dispatched[0].token == "repo:42:pull:5"
    assert bus.dispatched[0].state["kind"] == "pull"


async def test_review_comment_threads_on_root():
    payload = {
        "action": "created",
        "comment": {"id": 901, "in_reply_to_id": 800, "body": "@e2e-bot fix", "html_url": "u"},
        "pull_request": {"number": 9},
        "repository": {"id": 42, "full_name": "vercel/repo"},
        "sender": {"login": "andrey", "type": "User"},
    }
    _, bus = await handled(forwarded(payload, "pull_request_review_comment"))
    [inbound] = bus.dispatched
    assert inbound.token == "repo:42:pull:9:review-comment:800"
    assert inbound.state["kind"] == "review_thread"
    assert inbound.state["root_comment_id"] == 800


async def test_ignores_no_mention_bots_own_marker_and_other_events():
    cases = [
        forwarded(issue_comment(body="no mention here"), "issue_comment"),
        forwarded(issue_comment(sender={"login": "e2e-bot[bot]", "type": "Bot"}), "issue_comment"),
        forwarded(issue_comment(body=f"@e2e-bot hi\n\n{github.MARKER}"), "issue_comment"),
        forwarded(issue_comment(action="edited"), "issue_comment"),
        forwarded(issue_comment(), "issues"),
        forwarded(issue_comment(body="@e2e-bottle not us"), "issue_comment"),
    ]
    for webhook in cases:
        _, bus = await handled(webhook)
        assert bus.dispatched == []


async def test_retries_retain_source_id_for_server_dedupe():
    bus = FakeBus()
    await handled(forwarded(issue_comment(), "issue_comment", delivery="d1"), bus)
    await handled(forwarded(issue_comment(), "issue_comment", delivery="d1"), bus)
    await handled(forwarded(issue_comment(), "issue_comment", delivery="d2"), bus)
    assert [inbound.state["message_id"] for inbound in bus.dispatched] == [900] * 3
    assert bus.seen == set()


def api_channel(calls: list) -> github.GitHubChannel:
    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"id": 1})

    return github.channel(connector="github/e2e-bot", bot_name="e2e-bot", transport=httpx.MockTransport(responder))


def state(kind: str = "issue") -> dict:
    return {
        "owner": "vercel", "repo": "repo", "repository_id": 42, "kind": kind, "number": 5,
        "root_comment_id": 800 if kind == "review_thread" else None, "comment_id": 900,
    }


async def test_turn_started_reacts_eyes_with_connect_token(connect_stub):
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.TURN_STARTED), state())
    [request] = calls
    assert request.url.path == "/repos/vercel/repo/issues/comments/900/reactions"
    assert json.loads(request.read()) == {"content": "eyes"}
    assert request.headers["authorization"] == "Bearer ghs_connect"
    assert connect_stub == ["github/e2e-bot"]  # token minted from the connector




async def test_reply_posts_issue_comment_with_marker():
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.MESSAGE_COMPLETED, message="ported!"), state())
    [request] = calls
    assert request.url.path == "/repos/vercel/repo/issues/5/comments"
    assert json.loads(request.read())["body"] == f"ported!\n\n{github.MARKER}"


@pytest.mark.parametrize("origin,source", [("ui", "Hatchery UI"), ("slack", "Slack"), ("github", "GitHub")])
@pytest.mark.parametrize("kind", ["issue", "pull", "review_thread"])
async def test_human_message_mirrors_event_author_with_marker(origin, source, kind):
    calls: list[httpx.Request] = []
    response = await api_channel(calls).on_event(
        channels.event(channels.protocol.MESSAGE_RECEIVED, message="hello", author="Andrey", origin=origin),
        {**state(kind), "author": "Wrong Name", "sender_id": "someone-else"},
    )
    [request] = calls
    assert response == {"id": 1}
    assert json.loads(request.read())["body"] == f"Andrey · via {source}\n\nhello\n\n{github.MARKER}"
    expected_path = "/repos/vercel/repo/pulls/5/comments/800/replies" if kind == "review_thread" else "/repos/vercel/repo/issues/5/comments"
    assert request.url.path == expected_path


async def test_nonfinal_assistant_text_is_ignored_but_each_final_is_delivered():
    calls: list[httpx.Request] = []
    channel = api_channel(calls)
    result = await channel.on_event(
        channels.event(channels.protocol.MESSAGE_COMPLETED, message="working", final=False), state(),
    )
    assert result is None
    assert calls == []
    for text in ("first visible reply", "second visible reply"):
        assert await channel.on_event(
            channels.event(channels.protocol.MESSAGE_COMPLETED, message=text, final=True), state(),
        ) == {"id": 1}
    assert [json.loads(call.read())["body"] for call in calls] == [
        f"first visible reply\n\n{github.MARKER}", f"second visible reply\n\n{github.MARKER}",
    ]


async def test_failed_persistence_does_not_burn_retry():
    class FailingBus(FakeBus):
        async def dispatch(self, inbound):
            if not self.dispatched:
                self.dispatched.append(inbound)
                raise RuntimeError("store unavailable")
            await super().dispatch(inbound)

    bus = FailingBus()
    webhook = forwarded(issue_comment(), "issue_comment")
    with pytest.raises(RuntimeError, match="store unavailable"):
        await handled(webhook, bus)
    await handled(webhook, bus)
    assert [inbound.state["message_id"] for inbound in bus.dispatched] == [900, 900]
    assert not bus.seen


async def test_reply_to_review_thread_uses_replies_endpoint():
    calls: list[httpx.Request] = []
    await api_channel(calls).on_event(channels.event(channels.protocol.MESSAGE_COMPLETED, message="ok"), state("review_thread"))
    assert calls[0].url.path == "/repos/vercel/repo/pulls/5/comments/800/replies"


async def test_long_reply_is_chunked(monkeypatch):
    monkeypatch.setattr(github, "COMMENT_LIMIT", 40)
    calls: list[httpx.Request] = []
    size = 40 - len(github.MARKER) - 2
    await api_channel(calls).on_event(channels.event(channels.protocol.MESSAGE_COMPLETED, message="x" * (size + 1)), state())
    assert len(calls) == 2
    first, second = (json.loads(c.read())["body"] for c in calls)
    assert first == "x" * size + f"\n\n{github.MARKER}"
    assert second == "x" + f"\n\n{github.MARKER}"


async def test_api_error_raises():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible"})

    ch = github.channel(connector="github/e2e-bot", bot_name="e2e-bot", transport=httpx.MockTransport(responder))
    with pytest.raises(RuntimeError, match="403"):
        await ch.on_event(channels.event(channels.protocol.MESSAGE_COMPLETED, message="hi"), state())
