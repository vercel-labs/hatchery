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
        "sender": {"login": "andrey", "type": "User"},
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


async def test_dedupes_delivery_id():
    bus = FakeBus()
    await handled(forwarded(issue_comment(), "issue_comment", delivery="d1"), bus)
    await handled(forwarded(issue_comment(), "issue_comment", delivery="d1"), bus)
    await handled(forwarded(issue_comment(), "issue_comment", delivery="d2"), bus)
    assert len(bus.dispatched) == 2


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
