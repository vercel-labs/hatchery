"""channels: platform conversations in and out.

A channel is the edge adapter between a platform and the rest of the app: it
normalizes a webhook into an Inbound message, owns the token that maps a
platform conversation to its chat, and delivers events back to the platform.
Channels are framework-free: they take raw bytes/headers and return an Ack,
so they can be tested without an http server.

The App mounts channels on fastapi and hands inbound messages to a Hub — the
one seam to whatever stores chats and runs the agent. The App knows nothing
about storage; the hub owns dedupe and dispatch.

    import channels
    from channels import slack

    class Hub:
        async def dispatch(self, channel: str, inbound: channels.Inbound) -> None: ...
        async def dedupe(self, key: str) -> bool: ...

    bot = channels.App(Hub())
    bot.add(slack.channel())
    app.include_router(bot.router)

Outbound, a channel's `on_event` delivers one stream event to the platform
given the binding's state (thread ids, issue numbers); fanning events out to
bindings is the hub side's job.
"""

import dataclasses
import typing

import ai.experimental_telemetry
import fastapi

from channels import protocol
from channels.protocol import Event, Message, event

__all__ = ["Ack", "App", "Bus", "Channel", "Event", "Hub", "Inbound", "Message", "Webhook", "event"]


@dataclasses.dataclass
class Webhook:
    body: bytes
    headers: typing.Mapping[str, str]


@dataclasses.dataclass
class Inbound:
    token: str  # channel-local conversation address, e.g. "C123:1712.001"
    text: str  # model-visible text, wrapped with sender attribution
    state: dict  # channel state derived from this event
    title: str = ""  # chat title if this message opens a new chat
    repo: str | None = None  # "owner/repo" for project routing, github only
    persist: bool = True  # false when waking a chat after a separate sync
    invoke: bool = True  # persist every message; only some messages wake the agent


@dataclasses.dataclass
class Ack:
    status: int = 200
    body: str = '{"ok": true}'
    content_type: str = "application/json"
    work: typing.Coroutine | None = None  # runs after the ack is sent


class Bus(typing.Protocol):
    """What the app hands a channel's webhook handler."""

    async def dispatch(self, inbound: "Inbound") -> None: ...

    async def dedupe(self, key: str) -> bool: ...


class Hub(typing.Protocol):
    """Where inbound messages land; the store/agent side implements this."""

    async def dispatch(self, channel: str, inbound: "Inbound") -> None: ...

    async def dedupe(self, key: str) -> bool: ...


class Channel(typing.Protocol):
    name: str

    async def handle(self, webhook: "Webhook", bus: Bus) -> "Ack":
        """Verify, gate, normalize; ack fast and defer dispatch via Ack.work."""
        ...

    async def on_event(self, event: Event, state: dict) -> None:
        """Deliver one stream event back to the platform.

        state is the binding's channel state (thread ids, issue numbers).
        """
        ...


class App:
    """The one public object: mounts channels, feeds inbound messages to the hub."""

    def __init__(self, hub: Hub, prefix: str = "/channels/v1") -> None:
        self.hub = hub
        self.channels: dict[str, Channel] = {}
        self.router = fastapi.APIRouter(prefix=prefix)
        self.router.add_api_route("/{channel_name}", self._endpoint, methods=["POST"])

    def add(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    async def _endpoint(
        self, channel_name: str, request: fastapi.Request, background: fastapi.BackgroundTasks
    ) -> fastapi.Response:
        async with ai.experimental_telemetry.span("channel.webhook") as span:
            span.set_attrs(channel=channel_name)
            channel = self.channels.get(channel_name)
            if channel is None:
                span.set_attrs(status_code=404)
                return fastapi.Response('{"error": "unknown channel"}', 404, media_type="application/json")
            webhook = Webhook(body=await request.body(), headers=request.headers)
            ack = await channel.handle(webhook, _Bus(self.hub, channel.name))
            span.set_attrs(status_code=ack.status, dispatched=ack.work is not None)
            if ack.work is not None:
                background.add_task(_await, ack.work)
            return fastapi.Response(ack.body, ack.status, media_type=ack.content_type)


class _Bus:
    """Binds a channel name to the hub: keys and inbounds are channel-scoped."""

    def __init__(self, hub: Hub, channel: str) -> None:
        self._hub = hub
        self._channel = channel

    async def dispatch(self, inbound: Inbound) -> None:
        await self._hub.dispatch(self._channel, inbound)

    async def dedupe(self, key: str) -> bool:
        return await self._hub.dedupe(f"{self._channel}:{key}")


async def _await(coro: typing.Coroutine) -> None:
    await coro
