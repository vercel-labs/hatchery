"""Wire types shared by the event stream, channels, and the agent.

Port of eve's event envelope (protocol/message.ts), trimmed to what the
stream consumers need. Every event in a chat's stream has this shape; the UI
tails them raw, channel adapters pick the types they can deliver.

message.received is a user message (from any surface), message.completed an
assistant reply. status.updated is one driver-agnostic progress signal
(slack typing status; the UI status line; github ignores it).
"""

import datetime
import typing
import uuid

import pydantic

TURN_STARTED = "turn.started"
TURN_COMPLETED = "turn.completed"
TURN_FAILED = "turn.failed"
MESSAGE_RECEIVED = "message.received"
MESSAGE_COMPLETED = "message.completed"
STATUS_UPDATED = "status.updated"


class Meta(pydantic.BaseModel):
    id: str
    at: str


class Event(pydantic.BaseModel):
    type: str
    data: dict = {}
    meta: Meta


class Message(pydantic.BaseModel):
    role: typing.Literal["user", "assistant"]
    content: str


def event(type: str, **data) -> Event:
    at = datetime.datetime.now(datetime.UTC).isoformat()
    return Event(type=type, data=data, meta=Meta(id=f"evt_{uuid.uuid4().hex}", at=at))


def history(events: typing.Iterable[Event]) -> list[Message]:
    """Derive the conversation from a chat's event stream."""
    messages = []
    for ev in events:
        if ev.type == MESSAGE_RECEIVED:
            messages.append(Message(role="user", content=str(ev.data.get("message", ""))))
        elif ev.type == MESSAGE_COMPLETED:
            messages.append(Message(role="assistant", content=str(ev.data.get("message", ""))))
    return messages
