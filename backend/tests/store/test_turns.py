import pytest

from store import events, turns


async def test_active_folds_duplicate_and_late_lifecycle_records():
    await events.append(
        "chat_1",
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_old",
            "run_id": "run_old",
            "origin": "ui",
            "task_id": None,
        },
    )
    await events.append(
        "chat_1",
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_new",
            "run_id": "run_new",
            "origin": "worker",
            "task_id": "task_1",
        },
    )
    await events.append(
        "chat_1",
        "turns",
        {
            "type": "turn.completed",
            "turn_id": "turn_old",
            "run_id": "run_old",
        },
    )

    active = await turns.active("chat_1")
    assert active is not None
    assert active.turn_id == "turn_new"
    assert active.generation == 1

    await turns.finish("chat_1", "turn_new", "run_new", "completed")
    await turns.finish("chat_1", "turn_new", "run_new", "completed")
    assert await turns.active("chat_1") is None
    terminal = [
        data
        for _, data in await events.read("chat_1", "turns")
        if data.get("turn_id") == "turn_new" and data["type"] == "turn.completed"
    ]
    assert len(terminal) == 1


async def test_start_turn_rejects_an_active_owner(monkeypatch):
    from agent import durable

    class Run:
        run_id = "run_1"

    async def start(*_args):
        return Run()

    monkeypatch.setattr(durable.vercel.workflow, "start", start)
    await durable.start_turn("chat_1", "ui")
    with pytest.raises(turns.BusyError):
        await durable.start_turn("chat_1", "worker", "task_1")
