import ai

from agent import stream


async def test_to_sse_replays_agent_events_and_stops_at_terminal(monkeypatch):
    turn_id = "turn_1"

    async def readable(_run_id, start_index=0):
        assert start_index == 0
        yield stream.LifecycleEvent(
            type="turn.started", turn_id=turn_id, chat_id="chat_1"
        )
        message = ai.assistant_message("")
        message.id = "message_1"
        yield ai.events.StreamStart(message=message)
        yield ai.events.TextStart(message=message, block_id="part_1")
        yield ai.events.TextDelta(message=message, block_id="part_1", chunk="hello")
        yield ai.events.TextEnd(message=message, block_id="part_1")
        complete = ai.assistant_message("hello")
        complete.id = "message_1"
        yield ai.events.StreamEnd(message=complete)
        yield stream.LifecycleEvent(
            type="turn.completed", turn_id=turn_id, chat_id="chat_1"
        )
        raise AssertionError("read past terminal")

    monkeypatch.setattr(stream, "get_readable", readable)
    body = "".join([chunk async for chunk in stream.to_sse("run_1", turn_id)])

    assert '"type": "start"' in body
    assert '"delta": "hello"' in body
    assert body.endswith("data: [DONE]\n\n")


async def test_to_sse_emits_reload_marker(monkeypatch):
    async def readable(_run_id, start_index=0):
        yield stream.LifecycleEvent(
            type="reload.requested", turn_id="turn_1", chat_id="chat_1"
        )
        yield stream.LifecycleEvent(
            type="turn.failed", turn_id="turn_1", chat_id="chat_1"
        )

    monkeypatch.setattr(stream, "get_readable", readable)
    body = "".join([chunk async for chunk in stream.to_sse("run_1", "turn_1")])

    assert '"type": "data-reload"' in body
