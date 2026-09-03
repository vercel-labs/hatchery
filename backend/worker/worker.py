"""App-side Worker and task lifecycle."""

import asyncio
import datetime
import json
import secrets
import uuid

import ai.experimental_telemetry

from worker import models, protocol, queue, sandbox, store
from worker.daemon import VERSION as DAEMON_VERSION


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


async def _send_command(
    record: models.Worker,
    command: protocol.Command,
    parent: ai.experimental_telemetry.Span | None = None,
) -> None:
    async with ai.experimental_telemetry.use_span(parent):
        async with ai.experimental_telemetry.span("worker.command") as span:
            span.set_attrs(
                {
                    "chat.id": record.chat_id,
                    "worker.id": record.id,
                    "task.id": command.task_id or "",
                    "command.id": command.id,
                },
                command_type=command.type,
                sequence=command.sequence,
                worker_state=record.status,
            )
            await sandbox.prepare_for_command(record)
            message_id = await queue.send(command)
            span.set_attrs({"queue.message_id": message_id or ""})


async def create(
    chat_id: str, spec: models.WorkerSpec, *, user_id: str | None = None
) -> models.Worker:
    now = _now()
    worker_id = f"wrk_{uuid.uuid4().hex[:12]}"
    record = models.Worker(
        id=worker_id,
        chat_id=chat_id,
        user_id=user_id,
        sandbox_name=f"hatchery-{worker_id}",
        command_topic=protocol.command_topic(worker_id),
        title=spec.title,
        status="creating",
        spec=spec,
        daemon_token=secrets.token_urlsafe(32),
        created_at=now,
        updated_at=now,
    )
    await store.save(record)
    try:
        async with ai.experimental_telemetry.span("sandbox.provision") as span:
            span.set_attrs(
                {"chat.id": chat_id, "worker.id": record.id},
                repo_count=len(spec.repos),
                port_count=len(spec.ports),
            )
            if record.user_id is None:
                provisioned = await sandbox.provision(record.id, spec, record.daemon_token)
            else:
                provisioned = await sandbox.provision(
                    record.id, spec, record.daemon_token, user_id=record.user_id
                )
            span.set_attrs(sandbox_name=provisioned.sandbox_name)
    except Exception:
        record.status = "failed"
        record.updated_at = _now()
        await store.save(record)
        raise
    record.sandbox_name = provisioned.sandbox_name
    record.routes = provisioned.routes
    record.daemon_version = DAEMON_VERSION
    record.status = "running"
    record.updated_at = _now()
    return await store.save(record)


async def get(worker_id: str) -> models.Worker | None:
    return await store.get(worker_id)


async def list_all(chat_id: str | None = None) -> list[models.Worker]:
    return await store.list_all(chat_id)


async def stop(worker_id: str) -> models.Worker:
    record = await _required(worker_id)
    await sandbox.stop(record.sandbox_name)
    record.status = "stopped"
    record.updated_at = _now()
    return await store.save(record)


async def destroy(worker_id: str) -> None:
    record = await _required(worker_id)
    await sandbox.destroy(record.sandbox_name)
    for task in await store.list_tasks(record.chat_id):
        if task.worker_id == record.id:
            await store.delete_task(task.id)
    for terminal in await store.list_terminals(record.chat_id):
        if terminal.worker_id == record.id:
            await store.delete_terminal(terminal.id)
    await store.delete(record.id)


async def launch_task(
    chat_id: str,
    worker_id: str,
    prompt: str,
    model: str,
    *,
    task_id: str | None = None,
    command_id: str | None = None,
) -> models.Task:
    record = await _required(worker_id)
    if record.chat_id != chat_id:
        raise ValueError("sandbox does not belong to this chat")
    if record.status not in ("running", "stopped"):
        raise RuntimeError("sandbox is not available")
    resume_required = record.status == "stopped"
    now = _now()
    resolved_task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
    run_span = ai.experimental_telemetry.create_span("hatchery.agent_run").stamp_start()
    run_span.set_attrs(
        {
            "braintrust.input_json": json.dumps({"prompt": prompt}),
            "braintrust.span_attributes": json.dumps({"type": "task"}),
            "chat.id": chat_id,
            "worker.id": worker_id,
            "task.id": resolved_task_id,
        },
        model=model,
    )
    task = models.Task(
        id=resolved_task_id,
        chat_id=chat_id,
        worker_id=worker_id,
        title=prompt.strip().splitlines()[0][:80] or "subagent",
        prompt=prompt,
        model=model,
        telemetry_span=run_span.model_dump(mode="json") if run_span.id else None,
        inputs=[models.TaskInput(
            id=f"input_{uuid.uuid4().hex}",
            sequence=0,
            text=prompt,
            created_at=now,
        )],
        created_at=now,
        updated_at=now,
    )
    if not await store.create_task(task):
        raise ValueError("task already exists")
    command = protocol.command(
        worker_id,
        task.command_sequence,
        "task.launch",
        task_id=task.id,
        payload={"prompt": prompt, "model": model},
        command_id=command_id,
    )
    try:
        await _send_command(record, command, run_span)
    except Exception as error:
        task.launch_attempts += 1
        task.updated_at = _now()
        if run_span.id:
            run_span.set_attrs(task_state="launch_failed")
            run_span.stamp_end(error=error)
            task.telemetry_span = run_span.model_dump(mode="json")
            await run_span.push()
        await store.save_task(task)
        raise
    if resume_required:
        record.status = "running"
        record.updated_at = _now()
        await store.save(record)
    return task


async def send_task_input(chat_id: str, task_id: str, prompt: str) -> models.Task:
    if not prompt.strip():
        raise ValueError("task input must not be empty")
    current = await _required_task(chat_id, task_id)
    now = _now()

    def record(task: models.Task) -> models.Task:
        task.command_sequence += 1
        task.inputs.append(models.TaskInput(
            id=f"input_{uuid.uuid4().hex}",
            sequence=task.command_sequence,
            text=prompt,
            created_at=now,
        ))
        task.status = "pending"
        task.active_question = None
        task.active_question_id = None
        task.result = None
        task.completion_sequence = None
        task.completion_message = None
        task.completion_delivered = False
        task.updated_at = now
        return task

    task = await store.mutate_task(current.id, record)
    if task is None:
        raise KeyError(task_id)
    record = await _required(task.worker_id)
    parent = (
        ai.experimental_telemetry.Span[
            ai.experimental_telemetry.CustomSpanData
        ].model_validate(task.telemetry_span)
        if task.telemetry_span
        else None
    )
    await _send_command(
        record,
        protocol.command(
            task.worker_id,
            task.command_sequence,
            "task.input",
            task_id=task.id,
            payload={"prompt": prompt},
        ),
        parent,
    )
    return task


async def create_terminal(chat_id: str, worker_id: str) -> models.Terminal:
    record = await _required(worker_id)
    if record.chat_id != chat_id:
        raise ValueError("sandbox does not belong to this chat")
    if record.status != "running":
        raise RuntimeError("sandbox is not running")
    found = [item for item in await store.list_terminals(chat_id) if item.worker_id == worker_id]
    now = _now()
    terminal = models.Terminal(
        id=f"terminal_{uuid.uuid4().hex[:12]}",
        chat_id=chat_id,
        worker_id=worker_id,
        title=f"bash {len(found) + 1}",
        created_at=now,
        updated_at=now,
    )
    return await store.save_terminal(terminal)


async def delete_terminal(chat_id: str, terminal_id: str) -> None:
    terminal = await store.get_terminal(terminal_id)
    if terminal is None or terminal.chat_id != chat_id:
        raise ValueError("terminal does not belong to this chat")
    record = await _required(terminal.worker_id)
    await sandbox.tty_signal(record, terminal.id, "terminate")
    await store.delete_terminal(terminal.id)


async def list_terminals(chat_id: str) -> list[models.Terminal]:
    return await store.list_terminals(chat_id)


async def delete_task(chat_id: str, task_id: str) -> None:
    task = await cancel_task(chat_id, task_id)
    await store.delete_task(task.id)


async def cancel_task(chat_id: str, task_id: str) -> models.Task:
    task = await _required_task(chat_id, task_id)
    task.command_sequence += 1
    task.status = "cancelled"
    task.updated_at = _now()
    parent = (
        ai.experimental_telemetry.Span[
            ai.experimental_telemetry.CustomSpanData
        ].model_validate(task.telemetry_span)
        if task.telemetry_span
        else None
    )
    await store.save_task(task)
    record = await _required(task.worker_id)
    await _send_command(
        record,
        protocol.command(
            task.worker_id,
            task.command_sequence,
            "task.cancel",
            task_id=task.id,
        ),
        parent,
    )
    if parent is not None and parent.ended_at is None:
        parent.set_attrs(task_state=task.status)
        parent.stamp_end()
        task.telemetry_span = parent.model_dump(mode="json")
        await store.save_task(task)
        await parent.push()
    return task


async def launch_task_idempotent(
    chat_id: str,
    worker_id: str,
    prompt: str,
    model: str,
    request_id: str,
) -> models.Task:
    """Create a task once for a caller-supplied idempotency key."""
    task_id = f"task_{uuid.uuid5(uuid.NAMESPACE_URL, f'{worker_id}:{request_id}').hex[:12]}"
    existing = await store.get_task(task_id)
    if existing is not None:
        if (
            existing.chat_id != chat_id
            or existing.worker_id != worker_id
            or existing.prompt != prompt
            or existing.model != model
        ):
            raise ValueError("task idempotency key conflicts with an existing task")
        return existing
    return await launch_task(
        chat_id,
        worker_id,
        prompt,
        model,
        task_id=task_id,
        command_id=f"cmd_{uuid.uuid5(uuid.NAMESPACE_URL, f'{worker_id}:{request_id}:launch').hex}",
    )


async def record_input(task_id: str, text: str) -> models.Task:
    """Durably enqueue input without publishing it."""
    if not text.strip():
        raise ValueError("task input must not be empty")
    now = _now()

    def record(task: models.Task) -> models.Task:
        sequence = max((item.sequence for item in task.inputs), default=-1) + 1
        task.inputs.append(models.TaskInput(
            id=f"input_{uuid.uuid4().hex}", sequence=sequence, text=text, created_at=now
        ))
        task.updated_at = now
        return task

    task = await store.mutate_task(task_id, record)
    if task is None:
        raise KeyError(task_id)
    return task


async def allocate_event_sequence(task_id: str, source_id: str) -> int:
    """Return one stable sequence for a durable source event."""
    sequence = -1

    def allocate(task: models.Task) -> models.Task | None:
        nonlocal sequence
        if source_id in task.source_sequences:
            sequence = task.source_sequences[source_id]
            return None
        task.event_sequence += 1
        sequence = task.event_sequence
        task.event_ids.append(source_id)
        task.source_sequences[source_id] = sequence
        return task

    task = await store.mutate_task(task_id, allocate)
    if task is None:
        raise KeyError(task_id)
    return sequence


async def ask_question(task_id: str, question: str, source_id: str | None = None) -> models.Task:
    if not question.strip():
        raise ValueError("question must not be empty")
    now = _now()
    question_id = source_id or f"question_{uuid.uuid4().hex}"

    def ask(task: models.Task) -> models.Task | None:
        if task.status == "attention" and task.active_question == question:
            return None
        task.status = "attention"
        task.active_question = question
        task.active_question_id = question_id
        task.result = {"question": question}
        task.updated_at = now
        return task

    task = await store.mutate_task(task_id, ask)
    if task is None:
        raise KeyError(task_id)
    return task


async def answer_question(chat_id: str, task_id: str, answer: str) -> models.Task:
    """Record an answer as ordinary durable input and retire the active question."""
    task = await _required_task(chat_id, task_id)
    return await send_task_input(chat_id, task.id, answer)


async def complete_task(task_id: str, result: dict | str | None = None) -> models.Task:
    if isinstance(result, str):
        result = {"summary": result}
    return await complete_task_atomic(task_id, result or {})


async def complete_task_atomic(task_id: str, result: dict) -> models.Task:
    now = _now()

    def complete(task: models.Task) -> models.Task:
        task.status = "complete"
        task.active_question = None
        task.active_question_id = None
        task.result = result or {"summary": task.last_agent_words or "subagent completed"}
        task.updated_at = now
        return task

    task = await store.mutate_task(task_id, complete)
    if task is None:
        raise KeyError(task_id)
    return task


async def fail_task(task_id: str, reason: str) -> models.Task:
    now = _now()

    def fail(task: models.Task) -> models.Task:
        task.status = "errored"
        task.result = {"error": reason or "worker task failed"}
        task.updated_at = now
        return task

    task = await store.mutate_task(task_id, fail)
    if task is None:
        raise KeyError(task_id)
    return task


async def park_task(task_id: str, question: str | None = None) -> models.Task:
    task = await store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    prompt = question or task.active_question or task.last_agent_words or "input required"
    return await ask_question(task_id, prompt)


async def subscribe_task(task_id: str, after: int | None = None):
    """Yield durable task activity from a resumable cursor."""
    from store import events

    async for index, event in events.watch(task_id, "activity", 0 if after is None else after + 1):
        yield index, event


async def watch_task(chat_id: str, task_id: str, after: int | None = None):
    task = await _required_task(chat_id, task_id)
    from store import events

    start = 0 if after is None else after + 1
    existing = await events.read(task.id, "activity", start)
    for item in existing:
        yield item
    task = await store.get_task(task.id)
    if task is not None and task.status in ("complete", "errored", "cancelled"):
        return
    async for item in events.watch(task_id, "activity", start + len(existing)):
        yield item
        current = await store.get_task(task_id)
        if current is not None and current.status in ("complete", "errored", "cancelled"):
            return


async def reconcile_task(task_id: str) -> models.Task:
    """Drive durable pending input toward the sandbox Queue without changing semantics by environment."""
    task = await store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    if task.status in ("errored", "cancelled"):
        return task
    pending = [item for item in task.inputs if item.delivered_at is None]
    if not pending:
        return task
    kind = "task.launch" if task.event_sequence < 0 and pending[0].sequence == 0 else "task.input"
    text = "\n\n".join(item.text for item in pending)
    command = protocol.command(
        task.worker_id,
        task.command_sequence,
        kind,
        task_id=task.id,
        payload={"prompt": text, "model": task.model if kind == "task.launch" else ""},
        command_id=f"cmd_{uuid.uuid5(uuid.NAMESPACE_URL, f'{task.id}:{task.command_sequence}:{kind}').hex}",
    )
    try:
        record = await _required(task.worker_id)
        await _send_command(record, command)
    except Exception:
        task.launch_attempts += 1
        task.updated_at = _now()
        await store.save_task(task)
        raise
    now = _now()

    def delivered(current: models.Task) -> models.Task:
        ids = {item.id for item in pending}
        for item in current.inputs:
            if item.id in ids:
                item.delivered_at = now
        current.updated_at = now
        return current

    return await store.mutate_task(task.id, delivered) or task


async def get_task(chat_id: str, task_id: str | None = None) -> models.Task | None:
    if task_id is not None:
        task = await store.get_task(task_id)
        if task is None or task.chat_id != chat_id:
            return None
        return task
    tasks = await store.list_tasks(chat_id)
    return tasks[-1] if tasks else None


async def task_status(chat_id: str, task_id: str | None = None, after: int | None = None, limit: int = 20) -> dict:
    task = await get_task(chat_id, task_id)
    if task is None:
        return {"state": "idle", "events": [], "cursor": None}
    from store import events

    start = 0 if after is None else after + 1
    found = await events.read(task.id, "activity", start)
    bounded = found[: max(1, min(limit, 50))]
    return {
        "subagent_id": task.id,
        "task_id": task.id,
        "title": task.title,
        "state": task.status,
        "cursor": bounded[-1][0] if bounded else after,
        "has_more": len(found) > len(bounded),
        "events": [
            {
                "cursor": index,
                "kind": event.get("kind", "other"),
                "summary": event.get("summary", event.get("kind", "worker activity")),
            }
            for index, event in bounded
        ],
        "result": task.result,
    }


async def ingest(event: protocol.Event) -> tuple[models.Task | None, bool]:
    task, changed = await store.apply_event(event)
    if not changed or task is None:
        return task, changed
    from store import events

    await events.append(
        task.id,
        "activity",
        {
            "kind": event.type,
            "summary": _summary(event),
            "data": event.payload,
            "source_id": event.id,
            "sequence": event.sequence,
        },
    )
    await events.append(
        task.chat_id,
        "ui",
        {
            "type": "task.changed",
            "subagent_id": task.id,
            "sandbox_id": task.worker_id,
            "state": task.status,
        },
    )
    return task, changed


def _summary(event: protocol.Event) -> str:
    if event.type == "task.output":
        return str(event.payload.get("text") or "subagent update")[:500]
    if event.type == "task.question":
        return f"needs attention: {event.payload.get('question') or event.payload.get('text') or 'input required'}"[:500]
    return event.type.replace(".", " ")


async def _required(worker_id: str) -> models.Worker:
    record = await store.get(worker_id)
    if record is None:
        raise KeyError(worker_id)
    return record


async def _required_task(chat_id: str, task_id: str) -> models.Task:
    task = await store.get_task(task_id)
    if task is None or task.chat_id != chat_id:
        raise ValueError("subagent does not belong to this chat")
    return task
