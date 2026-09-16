import asyncio

import ai
import rotor
import rotor.testing

from agent import durable, runtime, stream
from app import server
from store import chats, events, spaces


async def _never_terminal(_process_id, _turn_id):
    await asyncio.Event().wait()


def _chunk(data, index=0):
    return rotor.Chunk(
        scope="chat_1",
        process_id="process_1",
        spawn_key="",
        activation=1,
        index=index,
        message_id=2,
        data=data,
    )


async def test_to_sse_replays_rotor_chunks_and_stops_at_terminal(monkeypatch):
    turn_id = "turn_1"

    async def readable(_process_id):
        message = ai.assistant_message("")
        message.id = "message_1"
        events = [
            ai.events.StreamStart(message=message),
            ai.events.TextStart(message=message, block_id="part_1"),
            ai.events.TextDelta(message=message, block_id="part_1", chunk="hello"),
            ai.events.TextEnd(message=message, block_id="part_1"),
            ai.events.StreamEnd(
                message=ai.assistant_message("hello").model_copy(
                    update={"id": "message_1"}
                )
            ),
        ]
        for index, event in enumerate(events):
            yield _chunk(
                {
                    "kind": "agent",
                    "turn_id": turn_id,
                    "event": event.model_dump(mode="json"),
                },
                index,
            )
        yield _chunk(
            {
                "kind": "lifecycle",
                "type": "turn.completed",
                "turn_id": turn_id,
                "chat_id": "chat_1",
            },
            len(events),
        )
        raise AssertionError("read past terminal")

    monkeypatch.setattr(stream, "get_readable", readable)
    monkeypatch.setattr(stream, "get_terminal", _never_terminal)
    body = "".join([chunk async for chunk in stream.to_sse("process_1", turn_id)])

    assert '"type": "start"' in body
    assert '"delta": "hello"' in body
    assert body.endswith("data: [DONE]\n\n")


async def test_to_sse_ignores_another_turn(monkeypatch):
    async def readable(_process_id):
        other = ai.events.TextDelta(
            message=ai.assistant_message(""), block_id="part_1", chunk="wrong"
        )
        yield _chunk(
            {
                "kind": "agent",
                "turn_id": "turn_other",
                "event": other.model_dump(mode="json"),
            }
        )
        yield _chunk(
            {
                "kind": "lifecycle",
                "type": "turn.completed",
                "turn_id": "turn_1",
                "chat_id": "chat_1",
            },
            1,
        )

    monkeypatch.setattr(stream, "get_readable", readable)
    monkeypatch.setattr(stream, "get_terminal", _never_terminal)
    body = "".join([chunk async for chunk in stream.to_sse("process_1", "turn_1")])

    assert "wrong" not in body
    assert body == "data: [DONE]\n\n"


async def test_to_sse_emits_reload_marker_for_discarded_activation(monkeypatch):
    async def readable(_process_id):
        yield rotor.Settled(
            scope="chat_1",
            process_id="process_1",
            spawn_key="",
            activation=1,
            message_id=2,
            outcome="discarded",
        )
        yield _chunk(
            {
                "kind": "lifecycle",
                "type": "turn.failed",
                "turn_id": "turn_1",
                "chat_id": "chat_1",
            }
        )

    monkeypatch.setattr(stream, "get_readable", readable)
    monkeypatch.setattr(stream, "get_terminal", _never_terminal)
    body = "".join([chunk async for chunk in stream.to_sse("process_1", "turn_1")])

    assert '"type": "data-reload"' in body


async def test_to_sse_finishes_from_durable_record_after_live_spool_settles(
    monkeypatch,
):
    space = await spaces.default()
    chat = await chats.create(space.id, "settled")
    user = ai.user_message("fast")
    await events.append(chat.id, "messages", user.model_dump(mode="json"))

    async def model_step(_history, _tools, _turn_id):
        return ai.assistant_message("done")

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(
        server,
        "_deliver",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as local:
        handle = await local.client.start(
            durable.DurableDispatcher,
            input={"chat_id": chat.id},
            key="dispatcher",
            scope=chat.id,
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_fast",
                "ui",
                [user.model_dump(mode="json")],
            )
        )
        await local.drain()
        monkeypatch.setattr(runtime, "client", local.client)
        body = "".join([chunk async for chunk in stream.to_sse(handle.id, "turn_fast")])

    assert '"type": "data-reload"' in body
    assert body.endswith("data: [DONE]\n\n")
