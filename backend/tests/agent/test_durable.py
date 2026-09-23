import asyncio

import ai
import httpx
import pytest
import rotor.testing

from hatchery.agent import durable
from hatchery.app import server
from hatchery.store import chats, events, spaces
from hatchery import worker


async def _chat(prompt: str = "help"):
    space = await spaces.default()
    chat = await chats.create(space.id, "dispatcher")
    message = ai.user_message(prompt)
    await events.append(chat.id, "messages", message.model_dump(mode="json"))
    return chat, message


def test_dispatcher_registers_only_rotor_processes():
    assert {process.__name__ for process in durable.PROCESSES} == {
        "DurableDispatcher",
        "announce_turn",
        "deliver_turn",
        "project_turn",
        "run_tool",
    }
    assert durable.DurableDispatcher.spool is True
    assert not hasattr(durable, "workflow")
    assert not hasattr(durable, "run_turn")


async def test_direct_answer_commits_and_returns_process_to_idle(monkeypatch):
    chat, message = await _chat()
    delivered = []

    async def model_step(history, tools, turn_id):
        assert history[-1].text == "help"
        assert turn_id == "turn_1"
        return ai.assistant_message("done")

    async def deliver(chat_id, text, *, final=True, delivery_key=None):
        delivered.append((chat_id, text, final, delivery_key))
        return []

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(server, "_deliver", deliver)
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher,
            input={"chat_id": chat.id},
            key="dispatcher",
            scope=chat.id,
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_1",
                "ui",
                [message.model_dump(mode="json")],
            ),
            idempotency_key="turn_1",
        )
        assert await runtime.drain() > 0
        activity, _ = await handle.query(durable.DurableDispatcher.activity)

    assert activity == {
        "chat_id": chat.id,
        "phase": "idle",
        "active_turn": None,
        "pending_turns": 0,
        "turns": 1,
        "error": "",
    }
    assert delivered == [(chat.id, "done", True, "turn_1:0")]
    transcript = await server._transcript(chat.id)
    assert [(item.role, item.text) for item in transcript] == [
        ("user", "help"),
        ("assistant", "done"),
    ]


async def test_model_tools_run_as_children_and_preserve_message_order(monkeypatch):
    chat, message = await _chat("inspect")
    calls = []

    async def model_step(history, tools, _turn_id):
        calls.append(({tool.name for tool in tools}, history[-1].role))
        if history[-1].role == "tool":
            assert history[-1].tool_results[0].result == []
            return ai.assistant_message("inspected")
        return ai.assistant_message(
            ai.messages.ToolCallPart(
                tool_call_id="call_1",
                tool_name="list_sandboxes",
                tool_args="{}",
            )
        )

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(
        server, "_deliver", lambda *_args, **_kwargs: asyncio.sleep(0, result=[])
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher,
            input={"chat_id": chat.id},
            key="dispatcher",
            scope=chat.id,
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_tools",
                "ui",
                [message.model_dump(mode="json")],
            )
        )
        await runtime.drain()

    transcript = await server._transcript(chat.id)
    assert [item.role for item in transcript] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert transcript[1].tool_calls[0].tool_call_id == "call_1"
    assert transcript[2].tool_results[0].tool_call_id == "call_1"
    assert transcript[3].text == "inspected"
    assert calls[0][1] == "user"
    assert calls[1][1] == "tool"


async def test_linked_chat_hides_start_thread_from_model(monkeypatch):
    chat, message = await _chat()
    offered = []

    async def model_step(_history, tools, _turn_id):
        offered.append({tool.name for tool in tools})
        return ai.assistant_message("done")

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(
        server, "_deliver", lambda *_args, **_kwargs: asyncio.sleep(0, result=[])
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher, input={"chat_id": chat.id}
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_linked",
                "ui",
                [message.model_dump(mode="json")],
                linked=True,
            )
        )
        await runtime.drain()

    assert "start_thread" not in offered[0]
    assert {"create_sandbox", "create_subagent", "read_notes"} <= offered[0]


async def test_duplicate_turn_delivery_runs_model_once(monkeypatch):
    chat, message = await _chat()
    model_calls = 0

    async def model_step(_history, _tools, _turn_id):
        nonlocal model_calls
        model_calls += 1
        return ai.assistant_message("done")

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(
        server, "_deliver", lambda *_args, **_kwargs: asyncio.sleep(0, result=[])
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher, input={"chat_id": chat.id}
        )
        turn = durable.TurnInput(
            chat.id,
            "turn_same",
            "ui",
            [message.model_dump(mode="json")],
        )
        assert await handle.send(turn, idempotency_key="turn_same") == "delivered"
        assert await handle.send(turn, idempotency_key="turn_same") == "duplicate"
        await runtime.drain()

    assert model_calls == 1


async def test_mailbox_queues_a_second_turn(monkeypatch):
    chat, first = await _chat("first")
    second = ai.user_message("second")
    await events.append(chat.id, "messages", second.model_dump(mode="json"))
    seen = []

    async def model_step(history, _tools, turn_id):
        seen.append((turn_id, history[-1].text))
        return ai.assistant_message(f"answer {history[-1].text}")

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(
        server, "_deliver", lambda *_args, **_kwargs: asyncio.sleep(0, result=[])
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher, input={"chat_id": chat.id}
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_1",
                "ui",
                [first.model_dump(mode="json")],
            )
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_2",
                "worker",
                [
                    first.model_dump(mode="json"),
                    second.model_dump(mode="json"),
                ],
            )
        )
        await runtime.drain()
        state, _ = await handle.read_state(durable.DurableDispatcher)

    assert seen == [("turn_1", "first"), ("turn_2", "second")]
    assert [
        ai.messages.Message.model_validate(item).text for item in state.messages
    ] == [
        "first",
        "answer first",
        "second",
        "answer second",
    ]


async def test_tool_task_uses_trusted_chat_context(monkeypatch):
    chat, _ = await _chat()
    seen = []

    async def list_all(chat_id):
        seen.append(chat_id)
        return []

    monkeypatch.setattr("hatchery.agent.sandbox.list_all", list_all)
    call = ai.messages.ToolCallPart(
        tool_call_id="call_1", tool_name="list_sandboxes", tool_args="{}"
    )
    async with rotor.testing.LocalRuntime(durable.run_tool) as runtime:
        handle = await runtime.client.start(
            durable.run_tool,
            input={
                "chat_id": chat.id,
                "turn_id": "turn_1",
                "actor_user_id": "user_actor",
                "call": call.model_dump(mode="json"),
            },
        )
        await runtime.drain()
        snapshot = await handle.snapshot()

    result = ai.messages.ToolResultPart.model_validate(snapshot.output)
    assert result.result == []
    assert seen == [chat.id]
    assert durable.list_sandboxes.tool.spec.params["properties"] == {}


async def test_delivery_finishes_worker_completion(monkeypatch):
    chat, _ = await _chat()
    task = worker.Task(
        id="task_1",
        chat_id=chat.id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        status="complete",
        event_sequence=2,
        completion_sequence=2,
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:00:00+00:00",
    )
    await worker.store.save_task(task)
    monkeypatch.setattr(
        server, "_deliver", lambda *_args, **_kwargs: asyncio.sleep(0, result=[])
    )
    final = ai.assistant_message("done")

    async with rotor.testing.LocalRuntime(durable.deliver_turn) as runtime:
        handle = await runtime.client.start(
            durable.deliver_turn,
            input={
                "chat_id": chat.id,
                "turn_id": "turn_1",
                "task_id": task.id,
                "replies": ["done"],
                "messages": [final.model_dump(mode="json")],
            },
        )
        await runtime.drain()
        assert (await handle.snapshot()).terminal_status == "completed"

    current = await worker.get_task(chat.id, task.id)
    assert current is not None
    assert current.completion_message == "done"
    assert current.completion_delivered is True
    assert (await chats.get(chat.id)).status == "done"


async def test_delivery_marks_latest_worker_record_without_overwriting_it(monkeypatch):
    chat, _ = await _chat()
    task = worker.Task(
        id="task_race",
        chat_id=chat.id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        status="complete",
        event_sequence=2,
        result={"summary": "old"},
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:00:00+00:00",
    )
    await worker.store.save_task(task)
    original_mutate = worker.store.mutate_task

    async def mutate_task(task_id, mutate):
        def receive_newer_event(current):
            current.status = "attention"
            current.event_sequence = 3
            current.result = {"question": "newer"}
            return current

        await original_mutate(task_id, receive_newer_event)
        return await original_mutate(task_id, mutate)

    monkeypatch.setattr(worker.store, "mutate_task", mutate_task)
    monkeypatch.setattr(
        server,
        "_deliver",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )
    final = ai.assistant_message("done")

    await durable.deliver_turn.fn(
        chat.id,
        "turn_1",
        task.id,
        ["done"],
        [final.model_dump(mode="json")],
    )

    current = await worker.get_task(chat.id, task.id)
    assert current is not None
    assert current.status == "attention"
    assert current.event_sequence == 3
    assert current.result == {"question": "newer"}
    assert current.completion_delivered is True


async def test_register_turn_is_idempotent_for_rotor_process():
    turn = durable.TurnInput("chat_1", "turn_1", "ui")

    assert await durable.register_turn(turn, "process_1") == 0
    assert await durable.register_turn(turn, "process_1") == 0

    assert len(await events.read("chat_1", "turns")) == 1
    assert (await events.read("chat_1", "ui"))[-1][1] == {
        "type": "stream.available",
        "turn_id": "turn_1",
        "run_id": "process_1",
        "generation": 0,
    }


async def test_register_turn_rejects_another_process_owner():
    turn = durable.TurnInput("chat_1", "turn_1", "ui")
    await durable.register_turn(turn, "process_1")

    with pytest.raises(RuntimeError, match="already owned"):
        await durable.register_turn(turn, "process_2")


async def test_delivery_retries_with_the_same_receipt_key(monkeypatch):
    chat, _ = await _chat()
    final = ai.assistant_message("done")
    attempts = []

    async def deliver(_chat_id, _text, *, final=True, delivery_key=None):
        attempts.append((final, delivery_key))
        return ["slack: unavailable"] if len(attempts) == 1 else []

    monkeypatch.setattr(server, "_deliver", deliver)
    async with rotor.testing.LocalRuntime(durable.deliver_turn) as runtime:
        handle = await runtime.client.start(
            durable.deliver_turn,
            input={
                "chat_id": chat.id,
                "turn_id": "turn_1",
                "task_id": None,
                "replies": ["done"],
                "messages": [final.model_dump(mode="json")],
            },
        )
        await runtime.drain()
        assert (await handle.snapshot()).phase == "idle"
        await runtime.advance("3s")
        snapshot = await handle.snapshot()

    assert snapshot.terminal_status == "completed"
    assert attempts == [(True, "turn_1:0"), (True, "turn_1:0")]
    assert len(await events.read(chat.id, "messages")) == 2


async def test_transient_model_failure_retries_from_durable_timer(monkeypatch):
    chat, message = await _chat()
    attempts = 0

    async def model_step(_history, _tools, _turn_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary")
        return ai.assistant_message("recovered")

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(
        server,
        "_deliver",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher, input={"chat_id": chat.id}
        )
        await handle.send(
            durable.TurnInput(
                chat.id,
                "turn_retry",
                "ui",
                [message.model_dump(mode="json")],
            )
        )
        await runtime.drain()
        activity, _ = await handle.query(durable.DurableDispatcher.activity)
        assert activity["phase"] == "generating"
        await runtime.advance("6s")
        activity, _ = await handle.query(durable.DurableDispatcher.activity)

    assert attempts == 2
    assert activity["phase"] == "idle"
    assert (await server._transcript(chat.id))[-1].text == "recovered"


async def test_terminal_projection_retries_before_dispatcher_returns_idle(monkeypatch):
    chat, message = await _chat()
    attempts = 0

    async def model_step(_history, _tools, _turn_id):
        return ai.assistant_message("done")

    from hatchery.store import turns

    original_finish = turns.finish

    async def finish(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("database unavailable")
        return await original_finish(*args, **kwargs)

    monkeypatch.setattr(durable, "model_step", model_step)
    monkeypatch.setattr(turns, "finish", finish)
    monkeypatch.setattr(
        server,
        "_deliver",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )
    async with rotor.testing.LocalRuntime(*durable.PROCESSES) as runtime:
        handle = await runtime.client.start(
            durable.DurableDispatcher, input={"chat_id": chat.id}
        )
        turn = durable.TurnInput(
            chat.id,
            "turn_projection",
            "ui",
            [message.model_dump(mode="json")],
        )
        await durable.register_turn(turn, handle.id)
        await handle.send(turn)
        await runtime.drain()
        activity, _ = await handle.query(durable.DurableDispatcher.activity)
        assert activity["phase"] == "finishing"
        await runtime.advance("3s")
        activity, _ = await handle.query(durable.DurableDispatcher.activity)

    assert attempts == 2
    assert activity["phase"] == "idle"
    assert await turns.active(chat.id) is None


async def test_active_turn_repairs_terminal_rotor_process(monkeypatch):
    from hatchery.agent import runtime

    await events.append(
        "chat_1",
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_1",
            "run_id": "process_1",
            "origin": "ui",
            "task_id": None,
        },
    )

    class Client:
        async def snapshot(self, _process_id):
            return type(
                "Snapshot",
                (),
                {"phase": "terminal", "failure": "crash loop"},
            )()

    monkeypatch.setattr(runtime, "client", Client())

    assert await durable.active_turn("chat_1") is None
    assert (await events.read("chat_1", "turns"))[-1][1]["type"] == "turn.failed"
