"""GitHub channel: @mentions in issue/pr comments in, comments out.

Connect-only. The GitHub App is owned by Vercel Connect: GitHub delivers
webhooks to Vercel, which forwards them here with a Vercel OIDC bearer token
that we verify instead of GitHub's HMAC. Installation tokens are minted per
call from Connect, so there is no app id, private key, or webhook secret to
hold. Inbound only works where Connect can reach the app — a deployment, not
a laptop; test on a preview deployment.

Setup (route must point at this channel, /channels/v1/github):

    vercel connect create github --triggers
    vercel connect detach <uid> --yes
    vercel connect attach <uid> --triggers --trigger-path /channels/v1/github --yes

then install the Connect-managed GitHub App on the target org from the
Connect dashboard. Tokens use the connector's default installation; selecting
between several installations (multi-org) is not wired up yet.

Behavior ported from eve's github channel defaults, trimmed:
- handle issue_comment and pull_request_review_comment with action "created"
- gate on an @bot_name token in the body (stripped before dispatch); drop
  bot senders and our own comments (hidden marker), dedupe delivery ids
- one session per conversation: "repo:<id>:issue:<n>", "repo:<id>:pull:<n>",
  or "repo:<id>:pull:<n>:review-comment:<root>" for review threads
- eyes reaction on turn start, chunked comments (65536 limit) on replies
"""

import json
import os
import re

import httpx
from vercel import connect

import channels

MARKER = "<!-- chat:github -->"
COMMENT_LIMIT = 65_536


def channel(
    connector: str | None = None,
    bot_name: str | None = None,
    name: str = "github",
    api_base: str = "https://api.github.com",
    transport: httpx.AsyncBaseTransport | None = None,
) -> "GitHubChannel":
    """connector is the Connect UID, e.g. "github/e2e-bot"; falls back to
    GITHUB_CONNECTOR. bot_name is the invocation token to watch for
    (@bot_name); falls back to GITHUB_APP_SLUG."""
    return GitHubChannel(connector, bot_name, name, api_base, transport)


class GitHubChannel:
    def __init__(
        self,
        connector: str | None,
        bot_name: str | None,
        name: str,
        api_base: str,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self.name = name
        self._connector = connector or os.environ.get("GITHUB_CONNECTOR", "")
        self._bot_name = bot_name or os.environ.get("GITHUB_APP_SLUG", "")
        self._client = httpx.AsyncClient(
            base_url=api_base,
            headers={"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"},
            transport=transport,
        )

    async def handle(self, webhook: channels.Webhook, bus: channels.Bus) -> channels.Ack:
        try:
            await connect.verify_connect_webhook(webhook.headers)
        except connect.ConnectWebhookVerificationError:
            return channels.Ack(401, '{"error": "unverified webhook"}')

        event_name = webhook.headers.get("x-github-event", "")
        if event_name == "ping":
            return channels.Ack()
        payload = json.loads(webhook.body)
        if event_name not in ("issue_comment", "pull_request_review_comment") or payload.get("action") != "created":
            return channels.Ack(200, '{"ok": true, "ignored": true}')

        inbound = self._gate(event_name, payload)
        if inbound is None:
            return channels.Ack(200, '{"ok": true, "ignored": true}')
        delivery = webhook.headers.get("x-github-delivery", "")
        if delivery and not await bus.dedupe(delivery):
            return channels.Ack()
        return channels.Ack(work=bus.dispatch(inbound))

    def _gate(self, event_name: str, payload: dict) -> channels.Inbound | None:
        comment = payload.get("comment") or {}
        sender = payload.get("sender") or {}
        body = comment.get("body", "")
        if not self._bot_name or sender.get("type") == "Bot" or MARKER in body:
            return None
        mention = re.compile(rf"@{re.escape(self._bot_name)}(?=$|[^A-Za-z0-9_-])", re.IGNORECASE)
        if not mention.search(body):
            return None
        message = mention.sub("", body).strip()

        repository = payload.get("repository") or {}
        repository_id = repository.get("id")
        owner, _, repo = repository.get("full_name", "").partition("/")
        if not repository_id or not owner or not repo:
            return None

        if event_name == "pull_request_review_comment":
            number = (payload.get("pull_request") or {}).get("number")
            root = comment.get("in_reply_to_id") or comment.get("id")
            kind = "review_thread"
            token = f"repo:{repository_id}:pull:{number}:review-comment:{root}"
        else:
            issue = payload.get("issue") or {}
            number = issue.get("number")
            root = None
            kind = "pull" if issue.get("pull_request") else "issue"
            token = f"repo:{repository_id}:{'pull' if kind == 'pull' else 'issue'}:{number}"
        if number is None:
            return None

        text = (
            f'<github_context repository="{owner}/{repo}" kind="{kind}" number="{number}"'
            f' sender="{sender.get("login", "")}" comment_url="{comment.get("html_url", "")}">\n'
            f"{message}\n</github_context>"
        )
        state = {
            "owner": owner,
            "repo": repo,
            "repository_id": repository_id,
            "kind": kind,
            "number": number,
            "root_comment_id": root,
            "comment_id": comment.get("id"),
        }
        return channels.Inbound(
            token=token,
            text=text,
            state=state,
            title=f"{owner}/{repo}#{number}",
            repo=f"{owner}/{repo}",
        )

    async def on_event(self, event: channels.Event, state: dict) -> None:
        if event.type == channels.protocol.TURN_STARTED:
            await self._react(state)
        elif event.type == channels.protocol.MESSAGE_COMPLETED:
            await self._comment(state, str(event.data.get("message", "")))
        elif event.type == channels.protocol.TURN_FAILED:
            await self._comment(state, f"something went wrong: {event.data.get('error', 'unknown error')}")

    async def _react(self, state: dict) -> None:
        comment_id = state.get("comment_id")
        if not comment_id:
            return
        subject = "pulls" if state["kind"] == "review_thread" else "issues"
        await self._api(
            "POST",
            f"/repos/{state['owner']}/{state['repo']}/{subject}/comments/{comment_id}/reactions",
            {"content": "eyes"},
        )

    async def _comment(self, state: dict, text: str) -> None:
        if not text:
            return
        if state["kind"] == "review_thread":
            path = (
                f"/repos/{state['owner']}/{state['repo']}/pulls/{state['number']}"
                f"/comments/{state['root_comment_id']}/replies"
            )
        else:
            path = f"/repos/{state['owner']}/{state['repo']}/issues/{state['number']}/comments"
        size = COMMENT_LIMIT - len(MARKER) - 2
        for start in range(0, len(text), size):
            await self._api("POST", path, {"body": f"{text[start : start + size]}\n\n{MARKER}"})

    async def _api(self, method: str, path: str, body: dict) -> dict:
        token = await connect.get_token(self._connector, subject=connect.ConnectAppTokenSubject())
        response = await self._client.request(method, path, json=body, headers={"authorization": f"Bearer {token}"})
        if response.status_code >= 300:
            raise RuntimeError(f"github {method} {path} failed: {response.status_code} {response.text[:200]}")
        return response.json() if response.content else {}
