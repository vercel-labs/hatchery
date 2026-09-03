"""Reconnectable workflow streams for durable dispatcher turns."""

import asyncio
import collections.abc
import contextlib
import typing

import ai
import ai.ui.ai_sdk
import ai.ui.ai_sdk.outbound_stream
import ai.ui.ai_sdk.ui_events
import pydantic
import vercel.workflow


class LifecycleEvent(pydantic.BaseModel):
    kind: typing.Literal["lifecycle"] = "lifecycle"
    type: typing.Literal[
        "turn.started",
        "reload.requested",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
    ]
    turn_id: str
    chat_id: str
    error: str | None = None


StreamEvent = ai.events.AgentEvent | LifecycleEvent
STREAM_EVENT_ADAPTER: pydantic.TypeAdapter[StreamEvent] = pydantic.TypeAdapter(StreamEvent)
_TERMINAL = {"turn.completed", "turn.failed", "turn.cancelled"}


def dump_event(event: StreamEvent) -> dict[str, typing.Any]:
    return event.model_dump(mode="json")


async def get_readable(
    run_id: str, start_index: int = 0
) -> collections.abc.AsyncIterator[StreamEvent]:
    """Replay and tail one workflow stream until its writer closes."""
    readable = vercel.workflow.Run(run_id).readable(start_index=start_index)
    async with contextlib.aclosing(readable):
        async for data in readable:
            yield STREAM_EVENT_ADAPTER.validate_python(data)


async def to_sse(run_id: str, turn_id: str) -> collections.abc.AsyncIterator[str]:
    """Translate one durable turn stream to AI SDK UI SSE."""
    queue: asyncio.Queue[
        ai.ui.ai_sdk.ui_events.UIMessageStreamEvent | Exception | None
    ] = asyncio.Queue()

    async def agent_events() -> collections.abc.AsyncIterator[ai.events.AgentEvent]:
        async for event in get_readable(run_id):
            if not isinstance(event, LifecycleEvent):
                yield event
                continue
            if event.turn_id != turn_id:
                continue
            if event.type == "reload.requested":
                await queue.put(ai.ui.ai_sdk.ui_events.UIFinishStepEvent())
                await queue.put(
                    ai.ui.ai_sdk.ui_events.UIDataEvent(data_type="reload", data={})
                )
                await queue.put(ai.ui.ai_sdk.ui_events.UIStartStepEvent())
            elif event.type in _TERMINAL:
                return

    async def pump() -> None:
        try:
            async for event in ai.ui.ai_sdk.to_stream(agent_events()):
                await queue.put(event)
        except Exception as error:
            await queue.put(error)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while (event := await queue.get()) is not None:
            if isinstance(event, Exception):
                raise event
            yield ai.ui.ai_sdk.outbound_stream.format_sse(event)
        yield ai.ui.ai_sdk.outbound_stream.format_done_sse()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
