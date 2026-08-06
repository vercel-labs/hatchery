"""The channel contract: the edge adapter between a platform and sessions.

A channel does three things (same contract as eve's defineChannel):
1. normalize a platform webhook into an Inbound message,
2. own the token that maps a platform conversation to its session,
3. deliver events (replies, progress, errors) back to the platform.

Channels are framework-free: they take raw bytes/headers and return an Ack,
so they can be tested without an http server. The app mounts them on fastapi
and runs `Ack.work` after the ack is sent — platforms expect a fast 200.
"""

import dataclasses
import typing

from chat import protocol, session


@dataclasses.dataclass
class Webhook:
    body: bytes
    headers: typing.Mapping[str, str]


@dataclasses.dataclass
class Inbound:
    token: str  # channel-local conversation address, e.g. "C123:1712.001"
    text: str  # model-visible text, wrapped with sender attribution
    state: dict  # channel state derived from this event


@dataclasses.dataclass
class Ack:
    status: int = 200
    body: str = '{"ok": true}'
    content_type: str = "application/json"
    work: typing.Coroutine | None = None  # runs after the ack is sent


class Bus(typing.Protocol):
    """What the app hands a channel's webhook handler."""

    async def dispatch(self, inbound: Inbound) -> session.Session: ...

    async def dedupe(self, key: str) -> bool: ...


class Channel(typing.Protocol):
    name: str

    async def handle(self, webhook: Webhook, bus: Bus) -> Ack:
        """Verify, gate, normalize; ack fast and defer dispatch via Ack.work."""
        ...

    async def on_event(self, event: protocol.Event, sess: session.Session) -> None:
        """Deliver one session event back to the platform.

        May mutate sess.channel_state; the app persists it after the turn.
        """
        ...
