"""chat: a small python port of eve's channel/session layer.

Connects platform conversations (slack threads, github issues/prs) to
durable sessions and delivers replies back. Indifferent to what produces
the replies — the handler can call a model or be plain code.

    import chat
    from chat.channels import slack

    async def handler(turn: chat.Turn) -> None:
        await turn.reply(f"you said: {turn.message.content}")

    bot = chat.App(handler)
    bot.add(slack.channel())
    app.include_router(bot.router)  # existing fastapi app
"""

import asyncio
import logging
import typing

import fastapi

from chat import protocol, session as session_mod
from chat.channel import Ack, Bus, Channel, Inbound, Webhook
from chat.protocol import Event, Message, event
from chat.session import MemoryStore, Session, Store

__all__ = [
    "Ack",
    "App",
    "Bus",
    "Channel",
    "Event",
    "Inbound",
    "MemoryStore",
    "Message",
    "Session",
    "Store",
    "Turn",
    "Webhook",
    "event",
]

log = logging.getLogger("chat")


class Turn:
    """One inbound message and the work it triggers. Handed to the handler."""

    def __init__(self, app: "App", channel: Channel, sess: Session, message: Message) -> None:
        self._app = app
        self._channel = channel
        self.session = sess
        self.message = message
        self.channel = channel.name

    async def reply(self, text: str) -> None:
        """Deliver a final reply to the conversation."""
        self.session.history.append(Message(role="assistant", content=text))
        await self._app._emit(self._channel, self.session, event(protocol.MESSAGE_COMPLETED, message=text))

    async def status(self, text: str) -> None:
        """Surface progress (slack typing status; github ignores it)."""
        await self._app._emit(self._channel, self.session, event(protocol.STATUS_UPDATED, status=text))


Handler = typing.Callable[[Turn], typing.Awaitable[None]]


class _Bus:
    def __init__(self, app: "App", channel: Channel) -> None:
        self._app = app
        self._channel = channel

    async def dispatch(self, inbound: Inbound) -> Session:
        return await self._app._dispatch(self._channel, inbound)

    async def dedupe(self, key: str) -> bool:
        return await self._app.store.dedupe(f"{self._channel.name}:{key}")


class App:
    """The one public object: mounts channels, routes messages to the handler."""

    def __init__(self, handler: Handler, store: Store | None = None, prefix: str = "/chat/v1") -> None:
        self.handler = handler
        self.store: Store = store if store is not None else MemoryStore()
        self.channels: dict[str, Channel] = {}
        self.router = fastapi.APIRouter(prefix=prefix)
        self._locks: dict[str, asyncio.Lock] = {}
        self.router.add_api_route("/{channel_name}", self._endpoint, methods=["POST"])

    def add(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

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

    async def _dispatch(self, channel: Channel, inbound: Inbound) -> Session:
        lock = self._locks.setdefault(f"{channel.name}:{inbound.token}", asyncio.Lock())
        async with lock:
            sess = await session_mod.resolve(self.store, channel.name, inbound.token, inbound.state)
            message = Message(role="user", content=inbound.text)
            sess.history.append(message)
            await self._emit(channel, sess, event(protocol.TURN_STARTED))
            try:
                await self.handler(Turn(self, channel, sess, message))
            except Exception as exc:
                log.exception("handler failed on %s", sess.token)
                await self._emit(channel, sess, event(protocol.TURN_FAILED, error=str(exc)))
            else:
                await self._emit(channel, sess, event(protocol.TURN_COMPLETED))
            await self.store.put(sess)
            return sess

    async def _emit(self, channel: Channel, sess: Session, ev: Event) -> None:
        # delivery failures must not corrupt the turn (same stance as eve)
        try:
            await channel.on_event(ev, sess)
        except Exception:
            log.exception("channel %s failed delivering %s", channel.name, ev.type)


async def _await(coro: typing.Coroutine) -> None:
    await coro
