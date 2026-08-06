"""Wire types shared by sessions, channels, and whoever drives replies.

Port of eve's event envelope (protocol/message.ts), trimmed to the events
the slack and github channels actually consume. `status.updated` replaces
eve's reasoning/action progress events with one driver-agnostic signal.
"""

import datetime
import typing
import uuid

import pydantic

TURN_STARTED = "turn.started"
TURN_COMPLETED = "turn.completed"
TURN_FAILED = "turn.failed"
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
