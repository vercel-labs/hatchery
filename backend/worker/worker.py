"""App-side Worker and task lifecycle."""

import datetime
import secrets
import uuid

from worker import models, protocol, queue, sandbox, store
from worker.daemon import VERSION as DAEMON_VERSION


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


async def create(chat_id: str, spec: models.WorkerSpec) -> models.Worker:
    now = _now()
    worker_id = f"wrk_{uuid.uuid4().hex[:12]}"
    record = models.Worker(
        id=worker_id,
        chat_id=chat_id,
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
        provisioned = await sandbox.provision(record.id, spec, record.daemon_token)
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
    await store.delete(record.id)


async def launch_task(
    chat_id: str, worker_id: str, prompt: str, model: str
) -> models.Task:
    record = await _required(worker_id)
    if record.chat_id != chat_id:
        raise ValueError("sandbox does not belong to this chat")
    if record.status == "stopped":
        await sandbox.resume(record.sandbox_name, record.id, record.spec, record.daemon_token)
        record.status = "running"
        record.updated_at = _now()
        await store.save(record)
    if record.status != "running":
        raise RuntimeError("sandbox is not running")
    now = _now()
    task = models.Task(
        id=f"task_{uuid.uuid4().hex[:12]}",
        chat_id=chat_id,
        worker_id=worker_id,
        title=prompt.strip().splitlines()[0][:80] or "subagent",
        prompt=prompt,
        model=model,
        created_at=now,
        updated_at=now,
    )
    await store.save_task(task)
    command = protocol.command(
        worker_id,
        task.command_sequence,
        "task.launch",
        task_id=task.id,
        payload={"prompt": prompt, "model": model},
    )
    await queue.send(command)
    return task


async def send_task_input(chat_id: str, task_id: str, prompt: str) -> models.Task:
    task = await _required_task(chat_id, task_id)
    task.command_sequence += 1
    task.status = "pending"
    task.result = None
    task.completion_delivered = False
    task.updated_at = _now()
    await store.save_task(task)
    await queue.send(
        protocol.command(
            task.worker_id,
            task.command_sequence,
            "task.input",
            task_id=task.id,
            payload={"prompt": prompt},
        )
    )
    return task


async def cancel_task(chat_id: str, task_id: str) -> models.Task:
    task = await _required_task(chat_id, task_id)
    task.command_sequence += 1
    task.status = "cancelled"
    task.updated_at = _now()
    await store.save_task(task)
    await queue.send(
        protocol.command(
            task.worker_id,
            task.command_sequence,
            "task.cancel",
            task_id=task.id,
        )
    )
    return task


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
