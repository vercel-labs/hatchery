"""channels: platform conversations in and out of chats.

A channel is the edge adapter between a platform and the chat store (same
contract as eve's defineChannel): it normalizes a webhook into an Inbound
message, owns the token that maps a platform conversation to its chat, and
delivers stream events back to the platform. Channels are framework-free:
they take raw bytes/headers and return an Ack, so they can be tested without
an http server.

The App mounts channels on fastapi and routes inbound messages: claim the
chat owning the token (creating it in the right project if new), append the
user message to the chat's event stream, and hand the turn to `start_turn` —
whatever runs the agent (the server wires this to a durable workflow).

    import channels
    from channels import slack

    async def start_turn(chat: chats.Chat, text: str) -> None: ...

    bot = channels.App(start_turn)
    bot.add(slack.channel())
    app.include_router(bot.router)

Events flow the other way through `emit`: append to the stream, then fan out
to every binding of the chat, so a reply lands in the UI, the slack thread,
and the github issue at once.
"""

import dataclasses
import logging
import typing

import fastapi

from channels import protocol
from channels.protocol import Event, Message, event
from store import chats, events, projects

__all__ = ["Ack", "App", "Bus", "Channel", "Event", "Inbound", "Message", "Webhook", "emit", "event"]

log = logging.getLogger("channels")


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


async def emit(registry: typing.Mapping[str, Channel], chat_id: str, ev: Event) -> None:
    """Append an event to the chat's stream and fan it out to every binding.

    Delivery failures must not corrupt the turn (same stance as eve): they are
    logged per binding and swallowed.
    """
    await events.append(chat_id, ev.model_dump())
    for binding in await chats.bindings(chat_id):
        channel = registry.get(binding.channel)
        if channel is None:
            continue  # binding without an adapter here (e.g. "cron")
        try:
            await channel.on_event(ev, binding.state)
        except Exception:
            log.exception("channel %s failed delivering %s to %s", binding.channel, ev.type, chat_id)


StartTurn = typing.Callable[[chats.Chat, str], typing.Awaitable[None]]


class App:
    """The one public object: mounts channels, routes messages into chats."""

    def __init__(self, start_turn: StartTurn, prefix: str = "/channels/v1") -> None:
        self.start_turn = start_turn
        self.channels: dict[str, Channel] = {}
        self.router = fastapi.APIRouter(prefix=prefix)
        self.router.add_api_route("/{channel_name}", self._endpoint, methods=["POST"])

    def add(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    async def emit(self, chat_id: str, ev: Event) -> None:
        await emit(self.channels, chat_id, ev)

    async def _endpoint(
        self, channel_name: str, request: fastapi.Request, background: fastapi.BackgroundTasks
    ) -> fastapi.Response:
        channel = self.channels.get(channel_name)
        if channel is None:
            return fastapi.Response('{"error": "unknown channel"}', 404, media_type="application/json")
        webhook = Webhook(body=await request.body(), headers=request.headers)
        ack = await channel.handle(webhook, _Bus(self, channel))
        if ack.work is not None:
            background.add_task(_await, ack.work)
        return fastapi.Response(ack.body, ack.status, media_type=ack.content_type)

    async def _dispatch(self, channel: Channel, inbound: Inbound) -> chats.Chat:
        project = (await projects.for_repo(inbound.repo)) if inbound.repo else None
        if project is None:
            project = await projects.get_default()
        token = f"{channel.name}:{inbound.token}"
        title = inbound.title or f"{channel.name} chat"
        chat, created = await chats.claim(token, channel.name, project.id, title, inbound.state)
        if not created:
            await chats.touch(chat.id)
        await self.emit(chat.id, event(protocol.MESSAGE_RECEIVED, message=inbound.text, channel=channel.name))
        try:
            await self.start_turn(chat, inbound.text)
        except Exception as exc:
            log.exception("start_turn failed on %s", token)
            await self.emit(chat.id, event(protocol.TURN_FAILED, error=str(exc)))
        return chat


class _Bus:
    def __init__(self, app: App, channel: Channel) -> None:
        self._app = app
        self._channel = channel

    async def dispatch(self, inbound: Inbound) -> None:
        await self._app._dispatch(self._channel, inbound)

    async def dedupe(self, key: str) -> bool:
        return await chats.dedupe(f"{self._channel.name}:{key}")


async def _await(coro: typing.Coroutine) -> None:
    await coro
