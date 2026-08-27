import pytest

from store import activity, events, subagents


async def test_legacy_local_records_are_removed_once(local_store):
    task_dir = local_store / "tasks"
    event_dir = local_store / "events"
    task_dir.mkdir(parents=True)
    event_dir.mkdir(parents=True)
    (task_dir / "launch_old.json").write_text("{}")
    (event_dir / "chat_1.messages.jsonl").write_text('{"old":true}\n')
    (event_dir / "chat_1.worker.jsonl").write_text('{"old":true}\n')
    (event_dir / "launch_old.activity.jsonl").write_text('{"old":true}\n')

    await subagents.ensure_ready()

    assert not task_dir.exists()
    assert not (event_dir / "chat_1.messages.jsonl").exists()
    assert not (event_dir / "chat_1.worker.jsonl").exists()
    assert not (event_dir / "launch_old.activity.jsonl").exists()
    assert (local_store / "subagents").is_dir()

    await events.append("chat_1", "messages", {"new": True})
    await subagents.ensure_ready()
    assert await events.read("chat_1", "messages") == [(0, {"new": True})]


async def test_tasks_are_independent_and_ordered_per_chat():
    first = await subagents.create("chat_1", "devbox_1", "first task", "one")
    second = await subagents.create("chat_1", "devbox_1", "second task", "two")
    await subagents.create("chat_2", "devbox_1", "other task", "three")

    first["task_id"] = "task_1"
    first["session_id"] = "session_1"
    first["state"] = "running"
    await subagents.save(first)

    listed = await subagents.list_for_chat("chat_1")
    assert [record["id"] for record in listed] == [first["id"], second["id"]]
    assert listed[0]["session_id"] == "session_1"
    assert listed[0]["devbox_id"] == "devbox_1"
    assert listed[1]["state"] == "creating"


async def test_finish_create_keeps_state_from_early_webhook():
    launch = await subagents.create("chat_1", "devbox_1", "task", "secret")
    launch["task_id"] = "task_1"
    launch["state"] = "running"
    await subagents.save(launch)

    saved = await subagents.finish_create(
        launch["id"],
        {"task_id": "task_1", "session_id": "session_1", "state": "pending"},
    )
    assert saved["state"] == "running"
    assert saved["session_id"] == "session_1"


async def test_activity_status_is_bounded_and_cursor_based():
    launch = await subagents.create("chat_1", "devbox_1", "inspect the bug", "secret")
    launch["task_id"] = "task_1"
    launch["state"] = "running"
    launch["result"] = {"summary": "secret until complete", "diffs": [{"patch": "huge"}]}
    await subagents.save(launch)
    await activity.append(
        launch["id"],
        "assistant_event",
        {
            "name": "tool_call",
            "body": {
                "tool": "Read",
                "input": {"kind": "read", "body": {"path": "backend/app/server.py"}},
            },
        },
        source_cursor="one",
    )
    await activity.append(
        launch["id"], "state_transition", {"from": "pending", "to": "running"}
    )

    first = await activity.status("chat_1", launch["id"], limit=1)
    assert first["events"] == [
        {"cursor": 0, "kind": "assistant_event", "summary": "Read: backend/app/server.py"}
    ]
    assert first["cursor"] == 0
    assert first["has_more"] is True
    assert first["result"] is None

    second = await activity.status("chat_1", launch["id"], after=first["cursor"])
    assert second["events"] == [
        {"cursor": 1, "kind": "state_transition", "summary": "state changed to running"}
    ]
    assert second["has_more"] is False


async def test_activity_status_returns_compact_terminal_result():
    launch = await subagents.create("chat_1", "devbox_1", "finish it", "secret")
    launch["state"] = "complete"
    launch["result"] = {
        "summary": "done",
        "diffs": [{"patch": "large and model-hostile"}],
        "prs": [{"url": "https://github.com/a/b/pull/1", "number": 1, "branch": "ignored"}],
    }
    await subagents.save(launch)

    status = await activity.status("chat_1", launch["id"])
    assert status["result"] == {
        "summary": "done",
        "prs": [{"url": "https://github.com/a/b/pull/1", "number": 1}],
    }


async def test_supervision_claim_is_serialized_and_generation_safe():
    launch = await subagents.create("chat_1", "devbox_1", "task", "secret")
    periodic = await subagents.claim_supervision(launch["id"], False)
    assert periodic is not None
    assert await subagents.claim_supervision(launch["id"], False) is None

    launch = await subagents.get(launch["id"])
    assert launch is not None
    launch["state"] = "complete"
    await subagents.save(launch)
    assert await subagents.claim_supervision(launch["id"], True) is None

    finished = await subagents.finish_supervision(
        launch["id"], periodic["supervision_generation"], completion_delivered=True
    )
    assert finished is not None
    assert finished["completion_delivered"] is True
    assert "supervision_lease_until" not in finished


async def test_activity_status_rejects_another_chats_launch():
    launch = await subagents.create("chat_2", "devbox_1", "other", "secret")
    with pytest.raises(ValueError, match="does not belong"):
        await activity.status("chat_1", launch["id"])
