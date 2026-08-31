"""Versioned messages exchanged by Hatchery and sandbox daemons."""

import datetime
import typing
import uuid

import pydantic

VERSION = 1
EVENT_TOPIC = "hatchery-worker-events-v1"

CommandType = typing.Literal["task.launch", "task.input", "task.cancel"]
EventType = typing.Literal[
    "daemon.ready",
    "task.started",
    "task.output",
    "task.transcript",
    "task.question",
    "task.completed",
    "task.failed",
]


class Envelope(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    version: typing.Literal[1] = VERSION
    id: str
    worker_id: str
    task_id: str | None = None
    sequence: int
    type: str
    created_at: str
    payload: dict[str, typing.Any] = {}


class Command(Envelope):
    type: CommandType


class Event(Envelope):
    type: EventType


def command_topic(worker_id: str) -> str:
    return f"hatchery-worker-{worker_id}-commands-v1"


def command(
    worker_id: str,
    sequence: int,
    type: CommandType,
    *,
    task_id: str | None = None,
    payload: dict[str, typing.Any] | None = None,
    command_id: str | None = None,
) -> Command:
    return Command(
        id=command_id or f"cmd_{uuid.uuid4().hex}",
        worker_id=worker_id,
        task_id=task_id,
        sequence=sequence,
        type=type,
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        payload=payload or {},
    )
