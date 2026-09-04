import datetime

import pytest

import models
from store import chats, jobs


def test_validate_schedule_accepts_only_five_fields():
    assert jobs.validate_schedule("  0  9 * * 1-5 ") == "0 9 * * 1-5"
    with pytest.raises(ValueError):
        jobs.validate_schedule("0 0 9 * * 1-5")
    with pytest.raises(ValueError):
        jobs.validate_schedule("not cron")


async def test_crud_and_owner_scoped_listing():
    created = await jobs.create("spc_1", "user_1", "0 9 * * 1-5", "Check reports")

    assert await jobs.list_for_space("spc_1", "user_2") == []
    assert (await jobs.list_for_space("spc_1", "user_1"))[0] == created

    updated = await jobs.update(created.id, "30 10 * * *", "Check builds")
    assert updated.schedule == "30 10 * * *"
    assert updated.prompt == "Check builds"
    assert (await jobs.set_paused(created.id, True)).paused is True
    assert (await jobs.set_paused(created.id, False)).paused is False
    assert await jobs.delete(created.id) is True
    assert await jobs.get(created.id) is None


async def test_claim_due_coalesces_and_is_idempotent(monkeypatch):
    job = await jobs.create("spc_1", "user_1", "* * * * *", "Run maintenance")
    due = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.UTC)
    job.next_run_at = (due - datetime.timedelta(hours=3)).isoformat()
    jobs._write_job(job)

    first = await jobs.claim_due(due)
    duplicate = await jobs.claim_due(due)

    assert len(first) == 1
    assert duplicate == []
    chat = await chats.get(first[0].chat_id)
    assert chat == models.Chat(
        id=chat.id,
        user_id="user_1",
        space_id="spc_1",
        title="Run maintenance",
        trigger=f"cron:{job.id}",
        created_at=chat.created_at,
    )
    advanced = await jobs.get(job.id)
    assert datetime.datetime.fromisoformat(advanced.next_run_at) > due
    transcript = await chats.get(first[0].chat_id)
    assert transcript is not None
    from store import events

    prompt_records = await events.read(first[0].chat_id, "messages")
    assert len(prompt_records) == 1
    assert prompt_records[0][1]["id"].startswith("job_prompt_")
    leased = await jobs.lease_pending(due)
    assert [execution.turn_id for execution in leased] == [first[0].turn_id]
    assert await jobs.lease_pending(due) == []

    assert await jobs.mark_started(leased[0], "run_1") is True
    assert await jobs.started_run(first[0].turn_id) == "run_1"


async def test_pause_and_delete_cancel_pending_executions():
    now = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.UTC)
    paused = await jobs.create("spc_1", "user_1", "* * * * *", "Pause me")
    paused.next_run_at = (now - datetime.timedelta(minutes=1)).isoformat()
    jobs._write_job(paused)
    paused_execution = (await jobs.claim_due(now))[0]

    await jobs.set_paused(paused.id, True)
    assert await jobs.lease_pending(now) == []
    assert await jobs.claim_run(paused_execution.turn_id, "run_late") is False
    assert await chats.get(paused_execution.chat_id) is None
    from store import events

    assert await events.read(paused_execution.chat_id, "messages") == []

    deleted = await jobs.create("spc_1", "user_1", "* * * * *", "Delete me")
    deleted.next_run_at = (now - datetime.timedelta(minutes=1)).isoformat()
    jobs._write_job(deleted)
    deleted_execution = (await jobs.claim_due(now))[0]

    await jobs.delete(deleted.id)
    assert await jobs.lease_pending(now) == []
    assert await jobs.claim_run(deleted_execution.turn_id, "run_late") is False
    assert await chats.get(deleted_execution.chat_id) is None
    assert await events.read(deleted_execution.chat_id, "messages") == []


async def test_pause_keeps_chat_after_turn_started():
    now = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.UTC)
    job = await jobs.create("spc_1", "user_1", "* * * * *", "Already started")
    job.next_run_at = (now - datetime.timedelta(minutes=1)).isoformat()
    jobs._write_job(job)
    execution = (await jobs.claim_due(now))[0]
    assert await jobs.claim_run(execution.turn_id, "run_1") is True

    await jobs.set_paused(job.id, True)

    assert await chats.get(execution.chat_id) is not None
    assert await jobs.started_run(execution.turn_id) == "run_1"


async def test_stable_turn_has_one_run_owner_and_cleanup_is_bounded():
    now = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.UTC)
    job = await jobs.create("spc_1", "user_1", "* * * * *", "Run once")
    job.next_run_at = (now - datetime.timedelta(days=40)).isoformat()
    jobs._write_job(job)
    execution = (await jobs.claim_due(now))[0]

    assert await jobs.claim_run(execution.turn_id, "run_1") is True
    assert await jobs.claim_run(execution.turn_id, "run_2") is False
    assert await jobs.cleanup(now) == 1
    assert await jobs.started_run(execution.turn_id) is None
