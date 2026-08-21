"""Slack channel: app mentions and DMs in, threaded replies out.

Connect-only. The Slack app is owned by Vercel Connect: Slack delivers events
to Vercel, which forwards them here with a Vercel OIDC bearer token that we
verify instead of Slack's HMAC. The bot token is minted per call from Connect,
so there is no SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET to hold. Inbound only
works where Connect can reach the app — a deployment, not a laptop; test on
a preview deployment.

Setup (route must point at this channel, /channels/v1/slack):

    vercel connect create slack --triggers
    vercel connect detach <uid> --yes
    vercel connect attach <uid> --triggers --trigger-path /channels/v1/slack --yes

Behavior ported from eve's slack channel defaults, trimmed:
- respond to app_mention and direct messages; ignore other traffic
- drop http_timeout retries, dedupe event_id durably, never reply to bots
- one session per thread: token is "<channel_id>:<thread_ts>"
- progress via assistant typing status, one post on the final reply
- slack web api calls are form-encoded on purpose: slack's json support is
  partial
"""

import html
import json
import os
import re

import httpx
from vercel import connect

import channels

STATUS_LIMIT = 50
TEXT_LIMIT = 40_000


def channel(
    connector: str | None = None,
    name: str = "slack",
    transport: httpx.AsyncBaseTransport | None = None,
) -> "SlackChannel":
    """connector is the Connect UID, e.g. "slack/e2e-bot"; falls back to SLACK_CONNECTOR."""
    return SlackChannel(connector, name, transport)


class SlackChannel:
    def __init__(self, connector: str | None, name: str, transport: httpx.AsyncBaseTransport | None) -> None:
        self.name = name
        self._connector = connector or os.environ.get("SLACK_CONNECTOR", "")
        self._client = httpx.AsyncClient(base_url="https://slack.com/api", transport=transport)
        self._profiles: dict[tuple[str, str], tuple[str, str]] = {}

    async def handle(self, webhook: channels.Webhook, bus: channels.Bus) -> channels.Ack:
        try:
            await connect.verify_connect_webhook(webhook.headers)
        except connect.ConnectWebhookVerificationError:
            return channels.Ack(401, '{"error": "unverified webhook"}')

        payload = json.loads(webhook.body)
        if payload.get("type") == "url_verification":
            return channels.Ack(200, str(payload.get("challenge", "")), "text/plain")
        retries = webhook.headers.get("x-slack-retry-num", "")
        if retries.isdigit() and int(retries) >= 1 and webhook.headers.get("x-slack-retry-reason") == "http_timeout":
            return channels.Ack()

        event = payload.get("event") or {}
        inbound = self._gate(payload, event)
        if inbound is None:
            return channels.Ack()
        event_id = payload.get("event_id")
        if event_id and not await bus.dedupe(str(event_id)):
            return channels.Ack()
        return channels.Ack(work=bus.dispatch(inbound))

    def _gate(self, payload: dict, event: dict) -> channels.Inbound | None:
        bot_user_id = ""
        for authorization in payload.get("authorizations") or []:
            bot_user_id = authorization.get("user_id", "")
            break
        if event.get("bot_id") or (bot_user_id and event.get("user") == bot_user_id):
            return None  # our own or another bot's message: never reply to bots
        kind = event.get("type")
        is_dm = kind == "message" and event.get("channel_type") == "im"
        if is_dm and event.get("subtype") not in (None, "file_share"):
            return None
        if kind != "app_mention" and not is_dm:
            return None

        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts") or ts
        if not channel_id or not ts:
            return None
        text = (
            f'<slack_message channel="{channel_id}" thread_ts="{thread_ts}" ts="{ts}"'
            f' sender="{event.get("user", "")}" team="{payload.get("team_id", "")}">\n'
            f"{event.get('text', '')}\n</slack_message>"
        )
        state = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "team_id": payload.get("team_id", ""),
            "user_id": event.get("user", ""),
        }
        title_text = re.sub(rf"<@{re.escape(bot_user_id)}>", "", str(event.get("text", "")))
        title_text = html.unescape(" ".join(title_text.split())).strip()
        title = f"slack: {title_text[:53]}" if title_text else "slack: thread"
        return channels.Inbound(token=f"{channel_id}:{thread_ts}", text=text, state=state, title=title)

    async def on_event(self, event: channels.Event, state: dict) -> None:
        if event.type == channels.protocol.TURN_STARTED:
            await self._set_status(state, "is thinking...")
        elif event.type == channels.protocol.STATUS_UPDATED:
            await self._set_status(state, str(event.data.get("status", ""))[:STATUS_LIMIT])
        elif event.type == channels.protocol.MESSAGE_RECEIVED and event.data.get("origin") == "ui":
            await self._post_ui_message(state, str(event.data.get("message", ""))[:TEXT_LIMIT])
        elif event.type == channels.protocol.MESSAGE_COMPLETED:
            await self._post(state, str(event.data.get("message", ""))[:TEXT_LIMIT])
        elif event.type == channels.protocol.TURN_FAILED:
            await self._post(state, f"something went wrong: {event.data.get('error', 'unknown error')}")

    async def _post_ui_message(self, state: dict, text: str) -> None:
        if not text:
            return
        key = (str(state.get("team_id", "")), str(state.get("user_id", "")))
        profile = self._profiles.get(key)
        if profile is None:
            body = await self._api("users.info", user=key[1])
            user = body.get("user") or {}
            details = user.get("profile") or {}
            name = details.get("display_name") or details.get("real_name") or user.get("real_name") or user.get("name") or "User"
            profile = (str(name), str(details.get("image_72") or details.get("image_48") or ""))
            self._profiles[key] = profile
        name, icon_url = profile
        params = {
            "channel": state["channel_id"],
            "thread_ts": state["thread_ts"],
            "text": text,
            "username": f"{name} · via Fabricator UI",
        }
        if icon_url:
            params["icon_url"] = icon_url
        await self._api("chat.postMessage", **params)

    async def _set_status(self, state: dict, status: str) -> None:
        await self._api(
            "assistant.threads.setStatus",
            channel_id=state["channel_id"],
            thread_ts=state["thread_ts"],
            status=status,
        )

    async def _post(self, state: dict, text: str) -> None:
        if text:
            await self._api("chat.postMessage", channel=state["channel_id"], thread_ts=state["thread_ts"], text=text)

    async def _api(self, method: str, **params: str) -> dict:
        token = await connect.get_token(self._connector, subject=connect.ConnectAppTokenSubject())
        response = await self._client.post(f"/{method}", data=params, headers={"authorization": f"Bearer {token}"})
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"slack {method} failed: {body.get('error', response.status_code)}")
        return body
