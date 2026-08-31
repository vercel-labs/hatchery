import asyncio

from worker import models, protocol, sandbox, worker


async def _worker(monkeypatch):
    async def provision(worker_id, spec, daemon_token):
        return sandbox.Provisioned(f"hatchery-{worker_id}", [])

    async def prepare_for_command(record):
        pass

    monkeypatch.setattr(worker.sandbox, "provision", provision)
    monkeypatch.setattr(worker.sandbox, "prepare_for_command", prepare_for_command)
    return await worker.create("chat_1", models.WorkerSpec(repos=["acme/app"]))


async def test_idempotent_launch_persists_and_publishes_one_identity(monkeypatch):
    record = await _worker(monkeypatch)
    sent = []

    async def send(command):
        sent.append(command)

    monkeypatch.setattr(worker.queue, "send", send)
    first = await worker.launch_task_idempotent(
        "chat_1", record.id, "fix it", "openai/test", "request_1"
    )
    second = await worker.launch_task_idempotent(
        "chat_1", record.id, "fix it", "openai/test", "request_1"
    )

    assert first == second
    assert len(sent) == 1
    assert sent[0].task_id == first.id
    assert first.inputs[0].text == "fix it"


async def test_question_answer_and_completion_are_atomic(monkeypatch):
    record = await _worker(monkeypatch)

    async def send(command):
        pass

    monkeypatch.setattr(worker.queue, "send", send)
    task = await worker.launch_task("chat_1", record.id, "fix it", "openai/test")
    task = await worker.ask_question(task.id, "Which file?")
    assert task.status == "attention"
    assert task.result == {"question": "Which file?"}

    task = await worker.answer_question("chat_1", task.id, "README.md")
    assert task.status == "pending"
    assert task.active_question is None
    assert task.inputs[-1].text == "README.md"

    task = await worker.complete_task_atomic(task.id, {"summary": "done"})
    assert task.status == "complete"
    assert task.result == {"summary": "done"}
    assert task.active_question is None


async def test_event_sequences_are_stable_and_concurrent(monkeypatch):
    record = await _worker(monkeypatch)

    async def send(command):
        pass

    monkeypatch.setattr(worker.queue, "send", send)
    task = await worker.launch_task("chat_1", record.id, "fix it", "openai/test")
    values = await asyncio.gather(
        worker.allocate_event_sequence(task.id, "source_a"),
        worker.allocate_event_sequence(task.id, "source_b"),
    )
    repeated = await worker.allocate_event_sequence(task.id, "source_a")

    assert sorted(values) == [0, 1]
    assert repeated == values[0]


async def test_watch_replays_then_emits_terminal_transition(monkeypatch):
    record = await _worker(monkeypatch)

    async def send(command):
        pass

    monkeypatch.setattr(worker.queue, "send", send)
    task = await worker.launch_task("chat_1", record.id, "fix it", "openai/test")
    started = protocol.Event(
        id="evt_started",
        worker_id=record.id,
        task_id=task.id,
        sequence=0,
        type="task.started",
        created_at="2026-08-28T00:00:00+00:00",
    )
    completed = protocol.Event(
        id="evt_completed",
        worker_id=record.id,
        task_id=task.id,
        sequence=1,
        type="task.completed",
        created_at="2026-08-28T00:00:01+00:00",
        payload={"result": {"summary": "done"}},
    )
    await worker.ingest(started)
    await worker.ingest(completed)

    found = [item async for item in worker.watch_task("chat_1", task.id)]
    assert [event["kind"] for _, event in found] == ["task.started", "task.completed"]
