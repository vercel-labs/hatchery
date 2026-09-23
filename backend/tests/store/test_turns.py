from hatchery.store import events, turns


async def test_active_folds_duplicate_and_late_lifecycle_records():
    await events.append(
        "chat_1",
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_old",
            "run_id": "process_1",
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
            "run_id": "process_1",
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
            "run_id": "process_1",
        },
    )

    active = await turns.active("chat_1")
    assert active is not None
    assert active.turn_id == "turn_new"
    assert active.run_id == "process_1"
    assert active.generation == 1

    await turns.finish("chat_1", "turn_new", "process_1", "completed")
    await turns.finish("chat_1", "turn_new", "process_1", "completed")
    assert await turns.active("chat_1") is None
    terminal = [
        data
        for _, data in await events.read("chat_1", "turns")
        if data.get("turn_id") == "turn_new" and data["type"] == "turn.completed"
    ]
    assert len(terminal) == 1
