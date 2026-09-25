"""AI SDK UI streaming over Rotor's reconnectable live spool."""

import asyncio
import collections.abc
import contextlib
import typing

import ai
import ai.ui.ai_sdk
import ai.ui.ai_sdk.outbound_stream
import ai.ui.ai_sdk.ui_events
import pydantic
import rotor


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
STREAM_EVENT_ADAPTER: pydantic.TypeAdapter[StreamEvent] = pydantic.TypeAdapter(
    StreamEvent
)
_TERMINAL = {"turn.completed", "turn.failed", "turn.cancelled"}


def dump_event(event: StreamEvent) -> dict[str, typing.Any]:
    return event.model_dump(mode="json")


async def get_readable(
    process_id: str,
) -> collections.abc.AsyncIterator[rotor.Chunk | rotor.Settled | rotor.Gap]:
    """Replay the in-flight spool, then follow one thread process live."""
    from hatchery.agent import runtime

    async for item in runtime.client.live(process_id=process_id, replay_inflight=True):
        yield item


async def get_terminal(process_id: str, turn_id: str) -> LifecycleEvent:
    """Wait for the turn's committed Rotor lifecycle record."""
    from hatchery.agent import runtime

    async for event in runtime.client.tail(process_id=process_id, poll="0.1s"):
        if event.kind not in _TERMINAL or not isinstance(event.data, dict):
            continue
        if event.data.get("turn_id") != turn_id:
            continue
        return LifecycleEvent(
            type=event.kind,
            turn_id=turn_id,
            chat_id=str(event.data.get("chat_id", "")),
            error=typing.cast(str | None, event.data.get("error")),
        )
    raise RuntimeError("Rotor event tail stopped before the turn completed")


async def to_sse(process_id: str, turn_id: str) -> collections.abc.AsyncIterator[str]:
    """Translate one Rotor turn's provisional events to AI SDK UI SSE."""
    queue: asyncio.Queue[
        ai.ui.ai_sdk.ui_events.UIMessageStreamEvent | Exception | None
    ] = asyncio.Queue()

    async def reload() -> None:
        await queue.put(ai.ui.ai_sdk.ui_events.UIFinishStepEvent())
        await queue.put(ai.ui.ai_sdk.ui_events.UIDataEvent(data_type="reload", data={}))
        await queue.put(ai.ui.ai_sdk.ui_events.UIStartStepEvent())

    async def agent_events() -> collections.abc.AsyncIterator[ai.events.AgentEvent]:
        readable = get_readable(process_id)
        live = asyncio.create_task(anext(readable))
        terminal = asyncio.create_task(get_terminal(process_id, turn_id))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {live, terminal}, return_when=asyncio.FIRST_COMPLETED
                )
                if live in done:
                    try:
                        item = live.result()
                    except StopAsyncIteration:
                        await terminal
                        await reload()
                        return
                    live = asyncio.create_task(anext(readable))
                elif terminal in done:
                    terminal.result()
                    await reload()
                    return
                else:
                    continue

                if isinstance(item, rotor.Gap):
                    await reload()
                    continue
                if isinstance(item, rotor.Settled):
                    if item.outcome in {"discarded", "unknown"}:
                        await reload()
                    continue
                if isinstance(item, rotor.Chunk):
                    data = item.data
                    if not isinstance(data, dict) or data.get("turn_id") != turn_id:
                        continue
                    if data.get("kind") == "agent":
                        event = STREAM_EVENT_ADAPTER.validate_python(data.get("event"))
                    elif data.get("kind") == "lifecycle":
                        event = STREAM_EVENT_ADAPTER.validate_python(data)
                    else:
                        continue
                else:
                    event = typing.cast(StreamEvent, item)

                if not isinstance(event, LifecycleEvent):
                    yield event
                    continue
                if event.turn_id != turn_id:
                    continue
                if event.type == "reload.requested":
                    await reload()
                elif event.type in _TERMINAL:
                    return
        finally:
            live.cancel()
            terminal.cancel()
            await asyncio.gather(live, terminal, return_exceptions=True)
            await readable.aclose()

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
