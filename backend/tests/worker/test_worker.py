import pytest

from worker import models, protocol, sandbox, worker
from worker.daemon import VERSION
from worker.daemon import main as daemon_main


async def test_create_provisions_and_persists(monkeypatch):
    async def provision(worker_id, spec, daemon_token):
        assert spec.repos == ["acme/app"]
        assert daemon_token
        return sandbox.Provisioned(
            sandbox_name=f"hatchery-{worker_id}",
            routes=[models.Route(port=3000, url="https://app.example")],
        )

    monkeypatch.setattr(worker.sandbox, "provision", provision)

    created = await worker.create(
        "chat_1", models.WorkerSpec(title="app", repos=["acme/app"], ports=[3000])
    )

    assert created.chat_id == "chat_1"
    assert created.status == "running"
    assert created.daemon_version == VERSION == daemon_main.VERSION
    assert created.daemon_token
    assert await worker.get(created.id) == created
    assert await worker.list_all("chat_1") == [created]
    assert await worker.list_all("chat_2") == []


async def test_create_records_failure(monkeypatch):
    worker_id = None

    async def provision(candidate, spec, daemon_token):
        nonlocal worker_id
        worker_id = candidate
        raise RuntimeError("boom")

    monkeypatch.setattr(worker.sandbox, "provision", provision)

    with pytest.raises(RuntimeError):
        await worker.create("chat_1", models.WorkerSpec())

    failed = await worker.get(worker_id)
    assert failed is not None
    assert failed.status == "failed"


async def test_launch_and_follow_up_publish_ordered_commands(monkeypatch):
    sent = []

    async def send(command):
        sent.append(command)

    async def refresh_queue_auth(record):
        pass

    monkeypatch.setattr(worker.queue, "send", send)
    monkeypatch.setattr(worker.sandbox, "refresh_queue_auth", refresh_queue_auth)
    async def provision(worker_id, spec, daemon_token):
        return sandbox.Provisioned(f"hatchery-{worker_id}", [])
    monkeypatch.setattr(worker.sandbox, "provision", provision)
    created = await worker.create("chat_1", models.WorkerSpec())

    task = await worker.launch_task("chat_1", created.id, "fix it", "openai/test")
    task.completion_delivered = True
    await worker.store.save_task(task)
    task = await worker.send_task_input("chat_1", task.id, "also test it")

    assert [command.type for command in sent] == ["task.launch", "task.input"]
    assert [command.sequence for command in sent] == [0, 1]
    assert task.command_sequence == 1
    assert task.completion_delivered is False
    with pytest.raises(ValueError, match="does not belong"):
        await worker.send_task_input("chat_2", task.id, "no")


async def test_stopped_worker_persists_task_before_resume_and_publish(monkeypatch):
    order = []

    async def provision(worker_id, spec, daemon_token):
        return sandbox.Provisioned(f"hatchery-{worker_id}", [])

    async def resume_for_command(record):
        assert await worker.store.list_tasks("chat_1")
        order.append("resume")

    async def refresh_queue_auth(record):
        order.append("refresh")

    async def send(command):
        assert (await worker.get(command.worker_id)).status == "running"
        order.append("send")

    monkeypatch.setattr(worker.sandbox, "provision", provision)
    monkeypatch.setattr(worker.sandbox, "resume_for_command", resume_for_command)
    monkeypatch.setattr(worker.sandbox, "refresh_queue_auth", refresh_queue_auth)
    monkeypatch.setattr(worker.queue, "send", send)
    created = await worker.create("chat_1", models.WorkerSpec())
    created.status = "stopped"
    await worker.store.save(created)

    task = await worker.launch_task("chat_1", created.id, "fix it", "openai/test")

    assert task.status == "pending"
    assert order == ["resume", "refresh", "send"]


async def test_ingest_is_idempotent_and_ordered(monkeypatch):
    async def send(command):
        pass

    async def refresh_queue_auth(record):
        pass

    monkeypatch.setattr(worker.queue, "send", send)
    monkeypatch.setattr(worker.sandbox, "refresh_queue_auth", refresh_queue_auth)
    async def provision(worker_id, spec, daemon_token):
        return sandbox.Provisioned(f"hatchery-{worker_id}", [])
    monkeypatch.setattr(worker.sandbox, "provision", provision)
    created = await worker.create("chat_1", models.WorkerSpec())
    task = await worker.launch_task("chat_1", created.id, "fix it", "openai/test")
    event = protocol.Event(
        id="evt_1", worker_id=created.id, task_id=task.id, sequence=1,
        type="task.completed", created_at="2026-08-28T00:00:00+00:00",
        payload={"summary": "done"},
    )

    applied, changed = await worker.ingest(event)
    duplicate, duplicate_changed = await worker.ingest(event)

    assert changed is True
    assert duplicate_changed is False
    assert applied == duplicate
    assert applied.status == "complete"
    assert applied.result == {"summary": "done"}
