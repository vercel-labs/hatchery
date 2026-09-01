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

In Connect's Advanced settings, add the message.channels trigger and the
channels:history bot scope. Private channels also need message.groups and
groups:history.

Behavior ported from eve's slack channel defaults, trimmed:
- respond to app mentions and DMs directly
- classify untagged thread replies against the full Slack thread
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
import urllib.parse

import ai
import httpx
import pydantic
from vercel import connect

import channels

STATUS_LIMIT = 50
TEXT_LIMIT = 40_000

THREAD_REPLY_SYSTEM = """\
Decide whether the newest Slack thread message is implicitly addressed to
Hatchery. Invoke Hatchery only when the message asks it for action or a decision
in the context of the thread. Do not invoke it for conversation between people,
acknowledgements, thanks, reactions, or status updates. A direct mention of
Hatchery is handled elsewhere. Return only the requested structured output."""


class ThreadReplyDecision(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    invoke: bool


def model() -> ai.Model:
    return ai.get_model("openai/gpt-5.6-luna")


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

        if webhook.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            form = urllib.parse.parse_qs(webhook.body.decode())
            payload = json.loads((form.get("payload") or ["{}"])[0])
        else:
            payload = json.loads(webhook.body)
        if payload.get("type") == "block_actions":
            return channels.Ack(400, '{"error": "unsupported interaction"}')
        if payload.get("type") == "url_verification":
            return channels.Ack(200, str(payload.get("challenge", "")), "text/plain")
        retries = webhook.headers.get("x-slack-retry-num", "")
        if retries.isdigit() and int(retries) >= 1 and webhook.headers.get("x-slack-retry-reason") == "http_timeout":
            return channels.Ack()

        event = payload.get("event") or {}
        inbound = self._gate(payload, event)
        candidate = self._thread_candidate(payload, event)
        if inbound is None and candidate is None:
            return channels.Ack()
        event_id = payload.get("event_id")
        if event_id and not await bus.dedupe(str(event_id)):
            return channels.Ack()
        if inbound is not None:
            return channels.Ack(work=bus.dispatch(inbound))
        return channels.Ack(work=self._classify_thread_reply(payload, event, bus))

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

        inbound = self._inbound(payload, event)
        if inbound is None:
            return None
        title_text = re.sub(rf"<@{re.escape(bot_user_id)}>", "", str(event.get("text", "")))
        title_text = html.unescape(" ".join(title_text.split())).strip()
        inbound.title = f"slack: {title_text[:53]}" if title_text else "slack: thread"
        return inbound

    def _thread_candidate(self, payload: dict, event: dict) -> channels.Inbound | None:
        bot_user_ids = {
            authorization.get("user_id", "")
            for authorization in payload.get("authorizations") or []
        }
        if event.get("bot_id") or event.get("user") in bot_user_ids:
            return None
        if event.get("type") != "message" or event.get("channel_type") == "im":
            return None
        if event.get("subtype") not in (None, "file_share") or not event.get("thread_ts"):
            return None
        text = str(event.get("text", ""))
        if any(user_id and f"<@{user_id}>" in text for user_id in bot_user_ids):
            return None  # app_mention is delivered separately for the same message
        return self._inbound(payload, event)

    def _inbound(self, payload: dict, event: dict) -> channels.Inbound | None:
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
        return channels.Inbound(token=f"{channel_id}:{thread_ts}", text=text, state=state)

    async def _classify_thread_reply(self, payload: dict, event: dict, bus: channels.Bus) -> None:
        inbound = self._thread_candidate(payload, event)
        if inbound is None:
            return
        body = await self._api(
            "conversations.replies",
            channel=inbound.state["channel_id"],
            ts=inbound.state["thread_ts"],
            limit="100",
        )
        messages = body.get("messages") or []
        transcript = [
            {"sender": message.get("user") or message.get("bot_id") or "unknown", "text": message.get("text", "")}
            for message in messages
        ]
        if await self._should_invoke(transcript, str(event.get("ts", ""))):
            await bus.dispatch(inbound)

    async def _should_invoke(self, transcript: list[dict], newest_ts: str) -> bool:
        agent = ai.Agent()
        request = json.dumps({"thread": transcript, "newest_ts": newest_ts}, ensure_ascii=False)
        async with agent.run(
            model(),
            [ai.system_message(THREAD_REPLY_SYSTEM), ai.user_message(request)],
            output_type=ThreadReplyDecision,
            params=ai.InferenceRequestParams(
                sampling={ai.TemperatureSamplerParams: ai.TemperatureSamplerParams(temperature=0)},
                output=ai.OutputParams(max_tokens=20),
            ),
        ) as result:
            async for _ in result:
                pass
            return result.output.invoke

    async def on_event(self, event: channels.Event, state: dict) -> None:
        if event.type == channels.protocol.TURN_STARTED:
            await self._set_status(state, "is thinking...")
        elif event.type == channels.protocol.SPACE_ASSIGNING:
            await self._set_status(state, "assigning a space...")
        elif event.type == channels.protocol.SPACE_ASSIGNED:
            await self._set_status(
                state, f"assigned {event.data.get('space', {}).get('name', 'space')}"
            )
        elif event.type == channels.protocol.STATUS_UPDATED:
            await self._set_status(state, str(event.data.get("status", ""))[:STATUS_LIMIT])
        elif event.type == channels.protocol.MESSAGE_RECEIVED and event.data.get("origin") == "ui":
            await self._post_ui_message(state, str(event.data.get("message", ""))[:TEXT_LIMIT])
        elif event.type == channels.protocol.MESSAGE_COMPLETED:
            if event.data.get("final", True):
                await self._post(state, str(event.data.get("message", ""))[:TEXT_LIMIT])
            else:
                await self._set_status(state, "is working...")
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
            "username": f"{name} · via Hatchery UI",
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
