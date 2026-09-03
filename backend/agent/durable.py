"""Durable dispatcher turns for unattended channel and worker wakeups."""

import collections.abc
import contextvars
import typing

import ai
import pydantic
import vercel.workflow


MODEL_ID = "openai/gpt-5.6-sol"

workflow = vercel.workflow.Workflows(
    sandbox_policy=vercel.workflow.SandboxPolicy(
        passthrough_modules=frozenset({"ai"}),
    )
)


class TurnInput(pydantic.BaseModel):
    chat_id: str
    turn_id: str = "turn_unknown"
    origin: typing.Literal["ui", "channel", "worker"]
    task_id: str | None = None


class PreparedTurn(pydantic.BaseModel):
    history: list[ai.messages.Message]
    cached_reply: str | None = None


@workflow.step
async def prepare_turn(turn: TurnInput) -> PreparedTurn:
    """Load canonical history and recover a committed worker reply if present."""
    from app import server
    from agent import dispatcher
    import worker

    stored = await server._transcript(turn.chat_id)
    cached_reply = None
    if turn.task_id is not None:
        task = await worker.get_task(turn.chat_id, turn.task_id)
        if task is None:
            raise ValueError("unknown subagent completion")
        cached_reply = task.completion_message
        if not cached_reply and task.completion_sequence is not None:
            marker = f"subagent_result_{task.id}_{task.completion_sequence}"
            result_index = next(
                (index for index, message in enumerate(stored) if message.id == marker),
                -1,
            )
            cached_reply = next(
                (
                    message.text
                    for message in reversed(stored[result_index + 1 :])
                    if message.role == "assistant" and message.text
                ),
                None,
            )
    space = await server._space_for_chat(turn.chat_id)
    return PreparedTurn(
        history=[ai.system_message(dispatcher.system_prompt(space)), *stored],
        cached_reply=cached_reply,
    )


@workflow.step
async def llm_step(
    context: ai.Context,
    writer: vercel.workflow.WorkflowWritable,
) -> ai.messages.Message:
    """Stream one retryable model step and return its complete message."""
    from agent import stream

    metadata = vercel.workflow.get_step_metadata()
    if metadata.attempt > 1:
        await writer.write(
            stream.dump_event(
                stream.LifecycleEvent(
                    type="reload.requested",
                    turn_id=current_agent.get().turn_id,
                    chat_id=current_agent.get().chat_id,
                )
            )
        )
    async with ai.stream(context=context) as model_stream:
        async for event in model_stream:
            if not event.replay:
                await writer.write(stream.dump_event(event))
    if model_stream.message is None:
        raise RuntimeError("model step returned no message")
    from agent import telemetry

    telemetry.flush()
    return model_stream.message


@workflow.step(max_retries=0)
async def create_sandbox_step(
    chat_id: str,
    repos: list[str],
    setup_script: str | None,
    ports: list[int],
    branch: str | None,
    git_sha: str | None,
    title: str,
) -> dict[str, typing.Any]:
    from agent import sandbox

    created = await sandbox.create(
        chat_id,
        sandbox.Launch(
            repos=repos,
            setup_script=setup_script,
            ports=ports,
            branch=branch,
            git_sha=git_sha,
            title=title,
        ),
    )
    return created.model_dump(exclude={"daemon_token"})


@workflow.step
async def list_sandboxes_step(chat_id: str) -> list[dict[str, typing.Any]]:
    from agent import sandbox

    return [
        item.model_dump(exclude={"daemon_token"})
        for item in await sandbox.list_all(chat_id)
    ]


@workflow.step(max_retries=0)
async def create_subagent_step(
    chat_id: str, sandbox_id: str, task: str, model: str
) -> dict[str, typing.Any]:
    from agent import sandbox

    created = await sandbox.launch_task(chat_id, sandbox_id, task, model)
    return {
        "subagent_id": created.id,
        "task_id": created.id,
        "sandbox_id": created.worker_id,
        "state": created.status,
    }


@workflow.step(max_retries=0)
async def message_subagent_step(
    chat_id: str, message: str, subagent_id: str | None
) -> dict[str, typing.Any]:
    from agent import sandbox
    import worker

    task = await worker.get_task(chat_id, subagent_id)
    if task is None:
        raise ValueError("no subagent can accept a message")
    updated = await sandbox.send_task_input(chat_id, task.id, message)
    return {"subagent_id": updated.id, "state": updated.status}


@workflow.step
async def check_subagent_step(
    chat_id: str, subagent_id: str | None, after: int | None, limit: int
) -> dict[str, typing.Any]:
    import worker

    return await worker.task_status(chat_id, subagent_id, after, limit)


current_agent: contextvars.ContextVar["DurableDispatcher"] = contextvars.ContextVar(
    "current_agent"
)


@ai.tool
async def create_sandbox(
    repos: list[str] | None = None,
    setup_script: str | None = None,
    ports: list[int] | None = None,
    branch: str | None = None,
    git_sha: str | None = None,
    title: str = "sandbox",
) -> dict[str, typing.Any]:
    """Create a persistent coding sandbox for this chat."""
    return await create_sandbox_step(
        current_agent.get().chat_id,
        list(repos or []),
        setup_script,
        list(ports or []),
        branch,
        git_sha,
        title,
    )


@ai.tool
async def list_sandboxes() -> list[dict[str, typing.Any]]:
    """List this chat's reusable coding sandboxes."""
    return await list_sandboxes_step(current_agent.get().chat_id)


@ai.tool
async def create_subagent(
    sandbox_id: str,
    task: str,
    model: str = MODEL_ID,
) -> dict[str, typing.Any]:
    """Start an fx subagent in a sandbox."""
    return await create_subagent_step(
        current_agent.get().chat_id, sandbox_id, task, model
    )


@ai.tool
async def message_subagent(
    message: str,
    subagent_id: str | None = None,
) -> dict[str, typing.Any]:
    """Send a revision, follow-up, or answer to an existing subagent."""
    return await message_subagent_step(
        current_agent.get().chat_id, message, subagent_id
    )


@ai.tool
async def check_subagent(
    subagent_id: str | None = None,
    after: int | None = None,
    limit: int = 20,
) -> dict[str, typing.Any]:
    """Read durable subagent state and recent events."""
    return await check_subagent_step(
        current_agent.get().chat_id, subagent_id, after, limit
    )


TOOLS = [
    create_sandbox,
    list_sandboxes,
    create_subagent,
    message_subagent,
    check_subagent,
]


class DurableDispatcher(ai.Agent):
    """Agent loop with streamed model steps and durable tool execution."""

    def __init__(
        self,
        chat_id: str,
        writer: vercel.workflow.WorkflowWritable,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(tools=TOOLS)
        self.chat_id = chat_id
        self.turn_id = turn_id or "turn_unknown"
        self.writer = writer

    async def loop(
        self, context: ai.Context
    ) -> collections.abc.AsyncGenerator[ai.events.AgentEvent]:
        while context.keep_running():
            assistant_message = await llm_step(context, self.writer)
            context.add(assistant_message)
            yield ai.events.StreamEnd(message=assistant_message)

            async with ai.ToolRunner() as runner:
                for tool_call in context.resolve(assistant_message.tool_calls):
                    runner.schedule(tool_call)
                async for event in runner.events():
                    await write_stream_event(self.writer, event)
                    yield event
                context.add(runner.get_tool_message())


@workflow.step
async def write_stream_event(
    writer: vercel.workflow.WorkflowWritable, event: ai.events.AgentEvent
) -> None:
    """Write an agent event to this workflow's reconnectable stream."""
    from agent import stream

    await writer.write(stream.dump_event(event))


@workflow.step
async def write_lifecycle_event(
    writer: vercel.workflow.WorkflowWritable, event: dict[str, typing.Any]
) -> None:
    await writer.write(event)


@workflow.step
async def register_turn(turn: TurnInput, run_id: str) -> int:
    """Idempotently register and announce a run from either side of startup."""
    from store import events, turns

    async with turns.run(turn.chat_id):
        records = await events.read(turn.chat_id, "turns")
        existing = next(
            (
                index
                for index, data in records
                if data.get("type") == "turn.started"
                and data.get("turn_id") == turn.turn_id
                and data.get("run_id") == run_id
            ),
            None,
        )
        generation = existing
        if generation is None:
            generation = await events.append(
                turn.chat_id,
                "turns",
                {
                    "type": "turn.started",
                    "turn_id": turn.turn_id,
                    "run_id": run_id,
                    "origin": turn.origin,
                    "task_id": turn.task_id,
                },
            )
        announced = any(
            data.get("type") == "stream.available"
            and data.get("turn_id") == turn.turn_id
            for _, data in await events.read(turn.chat_id, "ui")
        )
        if not announced:
            await events.append(
                turn.chat_id,
                "ui",
                {
                    "type": "stream.available",
                    "turn_id": turn.turn_id,
                    "run_id": run_id,
                    "generation": generation,
                },
            )
        return generation


@workflow.step
async def finish_turn(
    turn: TurnInput,
    run_id: str,
    state: typing.Literal["completed", "failed", "cancelled"],
    error: str | None = None,
) -> None:
    """Persist terminal ownership and UI invalidation outside workflow replay."""
    from store import events, turns

    await turns.finish(turn.chat_id, turn.turn_id, run_id, state, error)
    if state == "completed":
        await events.append(turn.chat_id, "ui", {"type": "messages.changed"})


@workflow.step
async def close_stream(writer: vercel.workflow.WorkflowWritable) -> None:
    await writer.close()


def lifecycle_event(
    event_type: str, turn: TurnInput, error: str | None = None
) -> dict[str, typing.Any]:
    """Build replay-safe lifecycle wire data without importing side-effect modules."""
    data: dict[str, typing.Any] = {
        "kind": "lifecycle",
        "type": event_type,
        "turn_id": turn.turn_id,
        "chat_id": turn.chat_id,
        "error": error,
    }
    return data


@workflow.step
async def commit_messages(
    chat_id: str, messages: list[ai.messages.Message]
) -> list[str]:
    """Idempotently append completed workflow messages to the canonical transcript."""
    from app import server
    from store import events

    known = {message.id for message in await server._transcript(chat_id)}
    for message in messages:
        if message.id not in known:
            await events.append(chat_id, "messages", message.model_dump(mode="json"))
            known.add(message.id)
    return [
        message.text
        for message in messages
        if message.role == "assistant" and message.text
    ] or ["subagent completion recorded"]


@workflow.step
async def ship_spans(spans: list[ai.experimental_telemetry.Span]) -> None:
    """Ship telemetry collected in the replayable workflow body."""
    await ai.experimental_telemetry.push_all(spans)


@workflow.step(max_retries=0)
async def emit_turn_event(chat_id: str, event_type: str, error: str | None = None) -> None:
    """Emit a channel lifecycle event outside replayable workflow code."""
    from app import server
    import channels

    data = {"error": error} if error is not None else {}
    await server._emit(chat_id, channels.event(event_type, **data))


@workflow.step(max_retries=0)
async def deliver_replies(turn: TurnInput, replies: list[str]) -> None:
    """Mirror replies to bound channels and finish worker completion bookkeeping."""
    from app import server
    from store import chats, events
    import worker

    for index, reply in enumerate(replies):
        failures = await server._deliver(
            turn.chat_id, reply, final=index == len(replies) - 1
        )
        if failures:
            raise RuntimeError("; ".join(failures))

    if turn.task_id is None:
        return
    task = await worker.get_task(turn.chat_id, turn.task_id)
    if task is None:
        return
    task.completion_message = replies[-1]
    task.completion_delivered = True
    await worker.store.save_task(task)
    if task.status in ("complete", "errored"):
        siblings = await worker.store.list_tasks(turn.chat_id)
        if not any(
            sibling.id != task.id
            and sibling.status in ("pending", "running", "attention")
            for sibling in siblings
        ):
            await chats.finish(
                turn.chat_id,
                "failed" if task.status == "errored" else "done",
                replies[-1],
            )
    await events.append(turn.chat_id, "ui", {"type": "messages.changed"})
    await events.append(turn.chat_id, "ui", {"type": "chat.changed"})


@workflow.workflow
@ai.messages.use_random(vercel.workflow.random)
@ai.experimental_telemetry.use_time(vercel.workflow.time_ns)
async def run_turn(turn: TurnInput) -> None:
    """Run, commit, and deliver one durable dispatcher turn."""
    writer = vercel.workflow.get_writable()
    run_id = vercel.workflow.get_workflow_metadata().run_id
    await register_turn(turn, run_id)
    await write_lifecycle_event(writer, lifecycle_event("turn.started", turn))
    await emit_turn_event(turn.chat_id, "turn.started")
    try:
        prepared = await prepare_turn(turn)
        if prepared.cached_reply:
            replies = [prepared.cached_reply]
        else:
            agent = DurableDispatcher(turn.chat_id, writer, turn.turn_id)
            collector = ai.experimental_telemetry.DictSink()
            token = current_agent.set(agent)
            try:
                async with (
                    ai.experimental_telemetry.use_sink(collector),
                    agent.run(ai.get_model(MODEL_ID), prepared.history) as result,
                ):
                    async for _ in result:
                        pass
                    added = result.messages[len(prepared.history) :]
            finally:
                current_agent.reset(token)
            if collector.finished_spans:
                await ship_spans(collector.finished_spans)
            replies = await commit_messages(turn.chat_id, added)
        await deliver_replies(turn, replies)
        await finish_turn(turn, run_id, "completed")
        await write_lifecycle_event(writer, lifecycle_event("turn.completed", turn))
    except Exception as error:
        await finish_turn(turn, run_id, "failed", str(error))
        await write_lifecycle_event(
            writer, lifecycle_event("turn.failed", turn, str(error))
        )
        await emit_turn_event(turn.chat_id, "turn.failed", str(error))
        raise
    finally:
        await close_stream(writer)


async def active_turn(chat_id: str) -> "turns.ActiveTurn | None":
    """Return a live owner and reconcile workflows that died before cleanup."""
    from store import turns

    active = await turns.active(chat_id)
    if active is None:
        return None
    status = await vercel.workflow.Run(active.run_id).status()
    if status in {"pending", "running"}:
        return active
    state: typing.Literal["completed", "failed", "cancelled"]
    state = status if status in {"completed", "failed", "cancelled"} else "failed"
    await turns.finish(chat_id, active.turn_id, active.run_id, state)
    return None


async def start_turn(
    chat_id: str,
    origin: typing.Literal["ui", "channel", "worker"],
    task_id: str | None = None,
) -> "turns.ActiveTurn":
    """Claim, start, register, and announce one durable dispatcher turn."""
    import uuid
    from store import turns

    async with turns.run(chat_id):
        if await active_turn(chat_id) is not None:
            raise turns.BusyError(f"chat {chat_id} already has an active turn")
        turn_id = f"turn_{uuid.uuid4().hex}"
        run = await vercel.workflow.start(
            run_turn,
            TurnInput(
                chat_id=chat_id,
                turn_id=turn_id,
                origin=origin,
                task_id=task_id,
            ),
        )
        payload = TurnInput(
            chat_id=chat_id,
            turn_id=turn_id,
            origin=origin,
            task_id=task_id,
        )
        generation = await register_turn.func(payload, run.run_id)
        return turns.ActiveTurn(turn_id, run.run_id, origin, task_id, generation)
