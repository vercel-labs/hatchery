from store import tasks


async def test_tasks_are_independent_and_ordered_per_chat():
    first = await tasks.create("chat_1", "first task", "one")
    second = await tasks.create("chat_1", "second task", "two")
    await tasks.create("chat_2", "other task", "three")

    first["task_id"] = "task_1"
    first["session_id"] = "session_1"
    first["state"] = "running"
    await tasks.save(first)

    listed = await tasks.list_for_chat("chat_1")
    assert [record["id"] for record in listed] == [first["id"], second["id"]]
    assert listed[0]["session_id"] == "session_1"
    assert listed[1]["state"] == "creating"


async def test_finish_create_keeps_state_from_early_webhook():
    launch = await tasks.create("chat_1", "task", "secret")
    launch["task_id"] = "task_1"
    launch["state"] = "running"
    await tasks.save(launch)

    saved = await tasks.finish_create(
        launch["id"],
        {"task_id": "task_1", "session_id": "session_1", "state": "pending"},
    )
    assert saved["state"] == "running"
    assert saved["session_id"] == "session_1"
