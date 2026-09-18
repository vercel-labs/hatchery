"""Rotor-backed durable dispatcher processes."""

import contextvars
import dataclasses
import json
import logging
import re
import typing
import uuid

import ai
import httpx
import pydantic
import rotor
import rotor.patterns
import store.turns


MODEL_ID = "openai/gpt-5.6-sol"
MAX_MEMORY_CONTENT_LENGTH = 1_000_000
log = logging.getLogger("agent.dispatcher")


@rotor.message
class TurnInput:
    chat_id: str
    turn_id: str
    origin: typing.Literal["ui", "channel", "worker", "cron"]
    history: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    linked: bool = False
    task_id: str | None = None
    actor_user_id: str | None = None


@rotor.message
class Generate:
    turn_id: str
    attempt: int = 0


@rotor.state
class DispatcherState:
    chat_id: str = ""
    messages: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    phase: str = "idle"
    active_turn: dict[str, typing.Any] | None = None
    pending_turns: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    linked: bool = False
    tool_calls: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    tool_results: dict[str, dict[str, typing.Any]] = dataclasses.field(
        default_factory=dict
    )
    tools: rotor.patterns.Fanout = dataclasses.field(
        default_factory=lambda: rotor.patterns.Fanout(name="tools")
    )
    replies: list[str] = dataclasses.field(default_factory=list)
    unprojected: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    turns: int = 0
    error: str = ""


@dataclasses.dataclass(frozen=True)
class ToolExecution:
    chat_id: str
    turn_id: str
    actor_user_id: str | None
    operation_key: str


_current_tool: contextvars.ContextVar[ToolExecution] = contextvars.ContextVar(
    "dispatcher_tool_execution"
)


def _execution() -> ToolExecution:
    try:
        return _current_tool.get()
    except LookupError as error:
        raise RuntimeError(
            "dispatcher tool called outside durable execution"
        ) from error


@ai.tool
async def create_sandbox(
    repos: list[str] | None = None,
    setup_script: str | None = None,
    ports: list[int] | None = None,
    branch: str | None = None,
    git_sha: str | None = None,
    title: str = "sandbox",
    size: typing.Literal["small", "big"] = "small",
) -> dict[str, typing.Any]:
    """Create a persistent sandbox owned by this chat and return its metadata.

    ``repos`` selects owner/repo working copies to clone; the first is primary.
    ``setup_script`` runs setup, ``ports`` exposes up to four ports, and ``branch``
    or ``git_sha`` selects the primary repo revision. Use small for research,
    reading, triage, light edits, and focused work. Use big for meaningful tests
    or builds, dev servers, browser/E2E work, monorepos, native compilation, or
    other heavy workloads. Files and processes persist across subagent runs.
    """
    from agent import sandbox

    execution = _execution()
    created = await sandbox.create(
        execution.chat_id,
        sandbox.Launch(
            repos=list(repos or []),
            setup_script=setup_script,
            ports=list(ports or []),
            branch=branch,
            git_sha=git_sha,
            title=title,
            size=size,
        ),
        actor_user_id=execution.actor_user_id,
        request_id=execution.operation_key,
    )
    return created.model_dump(exclude={"daemon_token"})


@ai.tool
async def list_sandboxes() -> list[dict[str, typing.Any]]:
    """Return metadata for this chat's reusable persistent sandboxes.

    This only reads sandbox state. Use the repository and status metadata to
    decide whether existing work can continue before creating another sandbox.
    """
    from agent import sandbox

    return [
        item.model_dump(exclude={"daemon_token"})
        for item in await sandbox.list_all(_execution().chat_id)
    ]


@ai.tool
async def create_subagent(
    sandbox_id: str,
    task: str,
    model: str = MODEL_ID,
    parent_subagent_id: str | None = None,
) -> dict[str, typing.Any]:
    """Start a fresh fx subagent chat in ``sandbox_id``.

    ``task`` is the context the new subagent receives. It can inspect and change
    the sandbox's persistent files and processes. The returned subagent/task ID,
    sandbox ID, and state mean the launch was accepted and work has started; they
    are not the completed result.
    """
    from agent import sandbox

    execution = _execution()
    created = await sandbox.launch_task(
        execution.chat_id,
        sandbox_id,
        task,
        model,
        actor_user_id=execution.actor_user_id,
        request_id=execution.operation_key,
        parent_task_id=parent_subagent_id,
    )
    return {
        "subagent_id": created.id,
        "task_id": created.id,
        "sandbox_id": created.worker_id,
        "state": created.status,
    }


@ai.tool
async def message_subagent(
    message: str,
    subagent_id: str | None = None,
) -> dict[str, typing.Any]:
    """Queue ``message`` for an existing subagent chat and resume its work.

    Use ``subagent_id`` to select the chat; omitting it targets this chat's most
    recently created subagent. The returned ID and state confirm that the durable
    queue accepted the message, not that the subagent answered it.
    """
    from agent import sandbox
    import worker

    execution = _execution()
    task = await worker.get_task(execution.chat_id, subagent_id)
    if task is None:
        raise ValueError("no subagent can accept a message")
    updated = await sandbox.send_task_input(
        execution.chat_id,
        task.id,
        message,
        actor_user_id=execution.actor_user_id,
        request_id=execution.operation_key,
    )
    return {"subagent_id": updated.id, "state": updated.status}


@ai.tool
async def check_subagent(
    subagent_id: str | None = None,
    after: int | None = None,
    limit: int = 20,
) -> dict[str, typing.Any]:
    """Return durable state and recent events for a subagent without changing it.

    ``subagent_id`` selects the chat, ``after`` requests events after a sequence,
    and ``limit`` bounds the event count from 1 to 50.
    """
    import worker

    return await worker.task_status(_execution().chat_id, subagent_id, after, limit)


@ai.tool
async def require_attention(
    reason: typing.Literal["result_available", "blocked"],
) -> dict[str, str]:
    """Set and return this chat's human-attention reason."""
    from store import chats, events

    chat_id = _execution().chat_id
    if await chats.set_attention(chat_id, reason) is None:
        raise ValueError("unknown chat")
    await events.append(chat_id, "ui", {"type": "chat.changed"})
    return {"reason": reason}


@ai.tool
async def find_channels(
    provider: typing.Literal["slack", "github"], query: str
) -> list[dict] | dict:
    """Search eligible destinations without sending a message."""
    from channels import destinations

    execution = _execution()
    try:
        return await destinations.find_channels(
            execution.chat_id, provider, query, execution.actor_user_id
        )
    except destinations.SlackScopeRequired as error:
        return error.result


@ai.tool
async def find_people(query: str) -> list[dict]:
    """Search linked people without sending anything and return person IDs."""
    from channels import destinations

    execution = _execution()
    return await destinations.find_people(
        execution.chat_id, query, execution.actor_user_id
    )


@ai.tool
async def start_thread(
    provider: typing.Literal["slack", "github"],
    destination: str,
    text: str,
    people: list[str] | None = None,
) -> dict:
    """Send the first notification and link its external thread to this chat."""
    from channels import destinations

    execution = _execution()
    return await destinations.start_thread(
        execution.chat_id,
        provider,
        destination,
        text,
        people,
        delivery_key=execution.operation_key,
        actor_user_id=execution.actor_user_id,
    )


def _storage_operation_id() -> str:
    return f"tool:{uuid.uuid5(uuid.NAMESPACE_URL, _execution().operation_key).hex}"


def _memory_path(path: str) -> str:
    value = path.strip()
    if not value.endswith(".md"):
        value += ".md"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,98}\.md", value):
        raise ValueError("memory path must be a lowercase kebab-case .md filename")
    return f"memories/{value}"


@ai.tool
async def read_memory(
    path: typing.Annotated[
        str | None,
        pydantic.Field(description="Memory filename to read, or omit to list memories"),
    ] = None,
) -> dict[str, typing.Any]:
    """List this agent's Git-backed memories or read one complete memory."""
    from app import server
    from store import agent_files

    agent = await server._agent_for_chat(_execution().chat_id)
    if not await agent_files.configured():
        return {"status": "unavailable", "message": "agent storage is not configured"}
    snapshot = await agent_files.snapshot(agent.slug)
    if path is None:
        return {
            "revision": snapshot.revision,
            "files": [item.removeprefix("memories/") for item in snapshot.files if item.startswith("memories/") and item != "memories/README.md"],
        }
    selected = _memory_path(path)
    found = await agent_files.read(agent.slug, selected, snapshot.revision)
    if found is None:
        return {"status": "not_found", "path": selected}
    return {"path": selected, "content": found.content, "revision": found.revision}


@ai.tool
async def read_skill(
    name: typing.Annotated[str, pydantic.Field(max_length=64)],
    file_path: typing.Annotated[str, pydantic.Field(max_length=200)] = "SKILL.md",
) -> dict[str, typing.Any]:
    """Read one complete Git-backed skill guide or contained support file."""
    from app import server
    from store import agent_files

    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name):
        raise ValueError("skill name must be a lowercase hyphenated slug")
    if not file_path or file_path.startswith("/") or ".." in file_path.split("/"):
        raise ValueError("skill file path must stay inside the skill directory")
    agent = await server._agent_for_chat(_execution().chat_id)
    if not await agent_files.configured():
        return {"status": "unavailable", "message": "agent storage is not configured"}
    selected = f"skills/{name}/{file_path}"
    found = await agent_files.read(agent.slug, selected)
    if found is None:
        return {"status": "not_found", "path": selected}
    return {"path": selected, "content": found.content, "revision": found.revision}


@ai.tool
async def create_memory(
    path: typing.Annotated[str, pydantic.Field(max_length=100)],
    content: typing.Annotated[
        str, pydantic.Field(max_length=MAX_MEMORY_CONTENT_LENGTH)
    ],
) -> dict[str, typing.Any]:
    """Create one durable Git-backed Markdown memory for this agent."""
    from app import server
    from store import agent_files

    agent = await server._agent_for_chat(_execution().chat_id)
    if not await agent_files.configured():
        return {"status": "unavailable", "message": "agent storage is not configured"}
    selected = _memory_path(path)
    snapshot = await agent_files.snapshot(agent.slug)
    if await agent_files.read(agent.slug, selected, snapshot.revision) is not None:
        return {"status": "exists", "path": selected, "revision": snapshot.revision}
    try:
        saved = await agent_files.write(
            agent.slug,
            selected,
            content,
            operation_id=_storage_operation_id(),
            expected_revision=snapshot.revision,
        )
    except agent_files.Conflict as error:
        return {"status": "conflict", "revision": error.current_revision}
    return {"status": "created", "path": selected, "revision": saved.revision}


@ai.tool
async def edit_memory(
    path: typing.Annotated[str, pydantic.Field(max_length=100)],
    find: typing.Annotated[
        str, pydantic.Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    ],
    replacement: typing.Annotated[
        str, pydantic.Field(max_length=MAX_MEMORY_CONTENT_LENGTH)
    ],
) -> dict[str, typing.Any]:
    """Replace one exact unique snippet in a Git-backed agent memory."""
    from app import server
    from store import agent_files

    agent = await server._agent_for_chat(_execution().chat_id)
    if not await agent_files.configured():
        return {"status": "unavailable", "message": "agent storage is not configured"}
    selected = _memory_path(path)
    found = await agent_files.read(agent.slug, selected)
    if found is None:
        return {"status": "not_found", "path": selected}
    count = found.content.count(find)
    if count != 1:
        return {"status": "not_unique", "matches": count}
    try:
        saved = await agent_files.write(
            agent.slug,
            selected,
            found.content.replace(find, replacement, 1),
            operation_id=_storage_operation_id(),
            expected_revision=found.revision,
        )
    except agent_files.Conflict as error:
        return {"status": "conflict", "revision": error.current_revision}
    return {"status": "saved", "path": selected, "revision": saved.revision}


BASE_TOOLS = [
    create_sandbox,
    list_sandboxes,
    create_subagent,
    message_subagent,
    check_subagent,
    require_attention,
    find_channels,
    find_people,
    read_memory,
    read_skill,
    create_memory,
    edit_memory,
]


def _tool_result(
    call: ai.messages.ToolCallPart, value: typing.Any, *, error: bool = False
) -> dict[str, typing.Any]:
    return ai.messages.ToolResultPart(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        result=value,
        result_kind="error" if error else "json",
    ).model_dump(mode="json")


@rotor.patterns.task
async def run_tool(
    chat_id: str,
    turn_id: str,
    actor_user_id: str | None,
    call: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    """Validate and execute one model tool call as a durable child process."""
    from agent import telemetry

    part = ai.messages.ToolCallPart.model_validate(call)
    if part.cached_result is not None:
        return part.cached_result.model_dump(mode="json")
    tool = next(
        (
            candidate
            for candidate in [*BASE_TOOLS, start_thread]
            if candidate.name == part.tool_name
        ),
        None,
    )
    if tool is None:
        return _tool_result(part, f"unknown tool: {part.tool_name}", error=True)
    if tool.require_approval:
        return _tool_result(part, "tool approval is not available", error=True)
    try:
        validator = tool.validator
        if validator is None:
            arguments = json.loads(part.tool_args)
        else:
            validated = validator.model_validate_json(part.tool_args)
            arguments = {
                name: getattr(validated, name) for name in type(validated).model_fields
            }
    except (json.JSONDecodeError, pydantic.ValidationError) as error:
        return _tool_result(
            part, f"invalid {part.tool_name} arguments: {error}", error=True
        )

    execution = ToolExecution(
        chat_id=chat_id,
        turn_id=turn_id,
        actor_user_id=actor_user_id,
        operation_key=rotor.idempotency_key(part.tool_call_id),
    )
    token = _current_tool.set(execution)
    try:
        async with telemetry.use_chat(chat_id):
            async with ai.experimental_telemetry.span(
                "hatchery.dispatcher.tool"
            ) as span:
                span.set_attrs(
                    {
                        "chat.id": chat_id,
                        "turn.id": turn_id,
                        "gen_ai.tool.call.id": part.tool_call_id,
                    },
                    tool_name=part.tool_name,
                )
                try:
                    result = await tool.fn(**arguments)
                except Exception as error:
                    span.set_attrs(tool_error=True)
                    return _tool_result(part, str(error), error=True)
                return _tool_result(part, result)
    finally:
        _current_tool.reset(token)
        telemetry.flush()


async def _persist_message(chat_id: str, message: dict[str, typing.Any]) -> str:
    from app import server
    from store import events

    parsed = ai.messages.Message.model_validate(message)
    if all(item.id != parsed.id for item in await server._transcript(chat_id)):
        await events.append(chat_id, "messages", message)
    return parsed.id


def _retry_projection(error: Exception, attempt: int) -> str | None:
    return f"{2**attempt}s" if attempt < 6 else None


@rotor.patterns.task(retry=_retry_projection)
async def project_turn(
    chat_id: str,
    turn_id: str,
    process_id: str,
    state: typing.Literal["completed", "failed"],
    error: str | None = None,
) -> None:
    """Project a terminal Rotor turn into Hatchery's lifecycle streams."""
    from store import events, jobs, turns

    await turns.finish(chat_id, turn_id, process_id, state, error)
    await jobs.mark_finished(turn_id, state)
    await events.append(chat_id, "ui", {"type": "messages.changed"})


@rotor.patterns.task
async def announce_turn(chat_id: str) -> None:
    """Emit channel thinking state after a turn enters the durable mailbox."""
    from app import server
    import channels

    await server._emit(chat_id, channels.event(channels.protocol.TURN_STARTED))


def _retry_delivery(error: Exception, attempt: int) -> str | None:
    return f"{2**attempt}s" if attempt < 4 else None


@rotor.patterns.task(retry=_retry_delivery)
async def deliver_turn(
    chat_id: str,
    turn_id: str,
    task_id: str | None,
    replies: list[str],
    messages: list[dict[str, typing.Any]],
) -> str:
    """Persist and deliver one committed final answer."""
    from app import server
    from store import chats, events
    import worker

    for message in messages:
        await _persist_message(chat_id, message)
    for index, reply in enumerate(replies):
        failures = await server._deliver(
            chat_id,
            reply,
            final=index == len(replies) - 1,
            delivery_key=f"{turn_id}:{index}",
        )
        if failures:
            raise RuntimeError("; ".join(failures))

    if task_id is not None:
        task = await worker.get_task(chat_id, task_id)
        if task is not None:

            def mark_delivered(current: worker.Task) -> worker.Task:
                current.completion_message = replies[-1]
                current.completion_delivered = True
                return current

            task = await worker.store.mutate_task(task.id, mark_delivered)
            if task is not None and task.status in ("complete", "errored"):
                siblings = await worker.store.list_tasks(chat_id)
                if not any(
                    sibling.id != task.id
                    and sibling.status in ("pending", "running", "attention")
                    for sibling in siblings
                ):
                    await chats.finish(
                        chat_id,
                        "failed" if task.status == "errored" else "done",
                        replies[-1],
                    )
            await events.append(chat_id, "ui", {"type": "chat.changed"})
    return replies[-1]


async def model_step(
    history: list[ai.messages.Message],
    tools: list[ai.AgentTool],
    turn_id: str,
) -> ai.messages.Message:
    """Run one model request and publish provisional AI SDK events through Rotor."""
    async with ai.stream(
        ai.get_model(MODEL_ID), history, tools=[tool.tool for tool in tools]
    ) as response:
        async for event in response:
            await rotor.stream(
                {
                    "kind": "agent",
                    "turn_id": turn_id,
                    "event": event.model_dump(mode="json"),
                }
            )
    if response.message is None:
        raise RuntimeError("model step returned no message")
    await rotor.stream(
        {
            "kind": "agent",
            "turn_id": turn_id,
            "event": ai.events.StreamEnd(message=response.message).model_dump(
                mode="json"
            ),
        }
    )
    return response.message


_TRANSIENT_MODEL_ERRORS = (
    httpx.TransportError,
    ai.errors.ProviderConnectionError,
    TimeoutError,
)


class DurableDispatcher(rotor.DurableProcess[DispatcherState]):
    """One long-lived, mailbox-serialized dispatcher per Hatchery chat."""

    spool = True
    handle_timeout = "300s"

    @rotor.on
    async def start(self, msg: rotor.Start) -> None:
        chat_id = str((msg.input or {}).get("chat_id", ""))
        if not chat_id:
            raise ValueError("dispatcher requires chat_id")
        self.state.chat_id = chat_id

    @rotor.on
    async def request_turn(self, msg: TurnInput) -> None:
        if msg.chat_id != self.state.chat_id:
            raise ValueError("turn belongs to another chat")
        if self.state.active_turn is not None:
            self.state.pending_turns.append(dataclasses.asdict(msg))
            rotor.record("turn.queued", {"turn_id": msg.turn_id, "origin": msg.origin})
            return
        self._begin(msg)

    def _begin(self, turn: TurnInput) -> None:
        known = {
            str(message.get("id"))
            for message in self.state.messages
            if message.get("id") is not None
        }
        for message in turn.history:
            message_id = str(message.get("id", ""))
            if message_id and message_id not in known:
                self.state.messages.append(message)
                known.add(message_id)
        self.state.active_turn = {
            "turn_id": turn.turn_id,
            "origin": turn.origin,
            "task_id": turn.task_id,
            "actor_user_id": turn.actor_user_id,
        }
        self.state.linked = turn.linked
        self.state.phase = "generating"
        self.state.error = ""
        self.state.replies.clear()
        self.state.tool_calls.clear()
        self.state.tool_results.clear()
        self.state.tools.clear()
        rotor.record("turn.started", {"turn_id": turn.turn_id, "origin": turn.origin})
        self.spawn(
            announce_turn,
            input={"chat_id": self.state.chat_id},
            key=f"announce:{turn.turn_id}",
            detached=True,
        )
        self.send(self.ref, Generate(turn.turn_id))

    @rotor.on
    async def generate(self, msg: Generate) -> None:
        active = self.state.active_turn
        if (
            active is None
            or active["turn_id"] != msg.turn_id
            or self.state.phase != "generating"
        ):
            return
        from app import server
        from agent import context, dispatcher, telemetry
        from store import agent_files

        agent = await server._agent_for_chat(self.state.chat_id)
        stored_context = "Agent storage is not configured."
        if await agent_files.configured():
            _, files = await agent_files.tree(agent.slug)
            stored_context = context.render(files)
        tools = [*BASE_TOOLS, *([] if self.state.linked else [start_thread])]
        history = [
            ai.system_message(
                dispatcher.system_prompt(
                    agent,
                    linked=self.state.linked,
                    stored_context=stored_context,
                )
            ),
            *[
                ai.messages.Message.model_validate(message)
                for message in self.state.messages
            ],
        ]
        try:
            async with telemetry.use_chat(self.state.chat_id):
                async with ai.experimental_telemetry.span(
                    "hatchery.dispatcher.turn"
                ) as span:
                    span.set_attrs(
                        {
                            "chat.id": self.state.chat_id,
                            "turn.id": msg.turn_id,
                        },
                        origin=str(active["origin"]),
                        actor_user_id=str(active.get("actor_user_id") or ""),
                        attempt=msg.attempt,
                    )
                    reply = await model_step(history, tools, msg.turn_id)
        except _TRANSIENT_MODEL_ERRORS as error:
            if msg.attempt >= 2:
                raise
            rotor.stream.discard()
            rotor.record(
                "model.retry",
                {
                    "turn_id": msg.turn_id,
                    "attempt": msg.attempt + 1,
                    "error": str(error),
                },
            )
            self.schedule(
                Generate(msg.turn_id, msg.attempt + 1),
                delay="5s",
                key=f"model-retry:{msg.turn_id}",
            )
            return
        finally:
            telemetry.flush()

        dumped = reply.model_dump(mode="json")
        self.state.messages.append(dumped)
        self.state.turns += 1
        if reply.text:
            self.state.replies.append(reply.text)
        rotor.record("model.turn", {"turn_id": msg.turn_id, "turn": self.state.turns})
        self.state.unprojected.append(dumped)
        if reply.tool_calls:
            self.state.phase = "tools"
            self.state.tool_calls = [
                call.model_dump(mode="json") for call in reply.tool_calls
            ]
            self.state.tool_results.clear()
            self.state.tools.clear()
            for call in self.state.tool_calls:
                key = self.state.tools.expect(data=call)
                self.spawn(
                    run_tool,
                    input={
                        "chat_id": self.state.chat_id,
                        "turn_id": msg.turn_id,
                        "actor_user_id": active.get("actor_user_id"),
                        "call": call,
                    },
                    key=key,
                )
            return

        replies = self.state.replies or ["dispatcher completed without a text reply"]
        self.state.phase = "delivering"
        self.spawn(
            deliver_turn,
            input={
                "chat_id": self.state.chat_id,
                "turn_id": msg.turn_id,
                "task_id": active.get("task_id"),
                "replies": replies,
                "messages": list(self.state.unprojected),
            },
            key=f"deliver:{msg.turn_id}",
        )

    async def _tool_settled(self, key: str, result: dict[str, typing.Any]) -> None:
        call = self.state.tools.settle(key=key)
        if call is None:
            return
        part = ai.messages.ToolResultPart.model_validate(result)
        self.state.tool_results[part.tool_call_id] = result
        if part.tool_name == start_thread.name and isinstance(part.result, dict):
            if part.result.get("status") == "sent":
                self.state.linked = True
        active = self.state.active_turn
        if active is not None:
            await rotor.stream(
                {
                    "kind": "agent",
                    "turn_id": active["turn_id"],
                    "event": ai.tool_result(part).model_dump(mode="json"),
                }
            )
        if not self.state.tools.settled:
            return
        parts = [
            ai.messages.ToolResultPart.model_validate(
                self.state.tool_results[
                    ai.messages.ToolCallPart.model_validate(item).tool_call_id
                ]
            )
            for item in self.state.tool_calls
        ]
        message = ai.tool_message(*parts)
        dumped = message.model_dump(mode="json")
        self.state.messages.append(dumped)
        self.state.unprojected.append(dumped)
        self.state.tool_calls.clear()
        self.state.tool_results.clear()
        self.state.tools.clear()
        self.state.phase = "generating"
        if active is not None:
            self.send(self.ref, Generate(str(active["turn_id"])))

    @rotor.on(run_tool.Done)
    async def tool_done(self, msg: rotor.ChildDone) -> None:
        await self._tool_settled(
            msg.key, typing.cast(dict[str, typing.Any], msg.output)
        )

    @rotor.on(run_tool.Failed)
    async def tool_failed(self, msg: rotor.ChildFailed) -> None:
        call = self.state.tools.pending.get(msg.key)
        if call is None:
            return
        part = ai.messages.ToolCallPart.model_validate(call)
        await self._tool_settled(
            msg.key, _tool_result(part, str(msg.reason), error=True)
        )

    @rotor.on(deliver_turn.Done)
    async def delivered(self, msg: rotor.ChildDone) -> None:
        active = self.state.active_turn
        if active is None or msg.key != f"deliver:{active['turn_id']}":
            return
        await self._complete(str(active["turn_id"]))

    @rotor.on(deliver_turn.Failed)
    async def delivery_failed(self, msg: rotor.ChildFailed) -> None:
        active = self.state.active_turn
        if active is None or msg.key != f"deliver:{active['turn_id']}":
            return
        await self._fail(str(msg.reason))

    @rotor.on
    async def handling_failed(self, msg: rotor.HandlingFailed) -> None:
        await self._fail(msg.error.detail)

    async def _complete(self, turn_id: str) -> None:
        try:
            await rotor.stream(
                {
                    "kind": "lifecycle",
                    "type": "turn.completed",
                    "turn_id": turn_id,
                    "chat_id": self.state.chat_id,
                }
            )
        except Exception:
            log.exception("failed to stream dispatcher completion for %s", turn_id)
        rotor.record(
            "turn.completed", {"turn_id": turn_id, "chat_id": self.state.chat_id}
        )
        self.state.phase = "finishing"
        self.spawn(
            project_turn,
            input={
                "chat_id": self.state.chat_id,
                "turn_id": turn_id,
                "process_id": self.ref.id,
                "state": "completed",
            },
            key=f"project:{turn_id}",
        )

    async def _fail(self, reason: str) -> None:
        active = self.state.active_turn
        if active is None:
            log.error("idle dispatcher failure for %s: %s", self.state.chat_id, reason)
            return
        turn_id = str(active["turn_id"])
        self.state.error = reason
        try:
            await rotor.stream(
                {
                    "kind": "lifecycle",
                    "type": "turn.failed",
                    "turn_id": turn_id,
                    "chat_id": self.state.chat_id,
                    "error": reason,
                }
            )
        except Exception:
            log.exception("failed to stream dispatcher failure for %s", turn_id)
        rotor.record(
            "turn.failed",
            {"turn_id": turn_id, "chat_id": self.state.chat_id, "error": reason},
        )
        for message in self.state.unprojected:
            try:
                await _persist_message(self.state.chat_id, message)
            except Exception:
                log.exception("failed to project dispatcher message for %s", turn_id)
        try:
            from app import server
            import channels

            await server._emit(
                self.state.chat_id,
                channels.event(channels.protocol.TURN_FAILED, error=reason),
                delivery_key=f"{turn_id}:failure",
            )
        except Exception:
            log.exception(
                "failed to emit dispatcher failure for %s", self.state.chat_id
            )
        self.state.phase = "finishing"
        self.spawn(
            project_turn,
            input={
                "chat_id": self.state.chat_id,
                "turn_id": turn_id,
                "process_id": self.ref.id,
                "state": "failed",
                "error": reason,
            },
            key=f"project:{turn_id}",
        )

    @rotor.on(project_turn.Done)
    async def projected(self, msg: rotor.ChildDone) -> None:
        active = self.state.active_turn
        if active is None or msg.key != f"project:{active['turn_id']}":
            return
        self._rest()

    @rotor.on(project_turn.Failed)
    async def projection_failed(self, msg: rotor.ChildFailed) -> None:
        active = self.state.active_turn
        if active is None or msg.key != f"project:{active['turn_id']}":
            return
        self.fail(msg.reason)

    def _rest(self) -> None:
        self.state.active_turn = None
        self.state.phase = "idle"
        self.state.tool_calls.clear()
        self.state.tool_results.clear()
        self.state.tools.clear()
        self.state.replies.clear()
        self.state.unprojected.clear()
        if self.state.pending_turns:
            self._begin(TurnInput(**self.state.pending_turns.pop(0)))

    @rotor.query
    def activity(self) -> dict[str, typing.Any]:
        return {
            "chat_id": self.state.chat_id,
            "phase": self.state.phase,
            "active_turn": self.state.active_turn,
            "pending_turns": len(self.state.pending_turns),
            "turns": self.state.turns,
            "error": self.state.error,
        }


PROCESSES: list[type[rotor.DurableProcess]] = [
    DurableDispatcher,
    run_tool,
    project_turn,
    announce_turn,
    deliver_turn,
]


async def _claim_turn(turn: TurnInput, process_id: str) -> None:
    """Claim stable turn identity before enqueuing work with external effects."""
    from store import events, jobs, turns

    async with turns.run(turn.chat_id):
        existing = next(
            (
                data
                for _, data in await events.read(turn.chat_id, "turns")
                if data.get("type") == "turn.started"
                and data.get("turn_id") == turn.turn_id
            ),
            None,
        )
        if existing is not None and existing.get("run_id") != process_id:
            raise RuntimeError(
                f"turn {turn.turn_id} is already owned by {existing.get('run_id')}"
            )
        if turn.origin == "cron" and not await jobs.claim_run(turn.turn_id, process_id):
            owner = await jobs.started_run(turn.turn_id)
            if owner != process_id:
                raise RuntimeError(
                    f"scheduled turn {turn.turn_id} is already owned by {owner}"
                )


async def register_turn(turn: TurnInput, process_id: str) -> int:
    """Project one accepted Rotor delivery into Hatchery's UI and cron stores."""
    from store import events, turns

    await _claim_turn(turn, process_id)
    async with turns.run(turn.chat_id):
        records = await events.read(turn.chat_id, "turns")
        existing = next(
            (
                (index, data)
                for index, data in records
                if data.get("type") == "turn.started"
                and data.get("turn_id") == turn.turn_id
            ),
            None,
        )
        if existing is None:
            generation = await events.append(
                turn.chat_id,
                "turns",
                {
                    "type": "turn.started",
                    "turn_id": turn.turn_id,
                    "run_id": process_id,
                    "origin": turn.origin,
                    "task_id": turn.task_id,
                    "actor_user_id": turn.actor_user_id,
                },
            )
        else:
            generation = existing[0]
        if not any(
            data.get("type") == "stream.available"
            and data.get("turn_id") == turn.turn_id
            for _, data in await events.read(turn.chat_id, "ui")
        ):
            await events.append(
                turn.chat_id,
                "ui",
                {
                    "type": "stream.available",
                    "turn_id": turn.turn_id,
                    "run_id": process_id,
                    "generation": generation,
                },
            )
        return generation


async def active_turn(chat_id: str) -> store.turns.ActiveTurn | None:
    """Return a live turn and repair projections for a terminal process."""
    from agent import runtime
    from store import turns

    active = await turns.active(chat_id)
    if active is None:
        return None
    try:
        snapshot = await runtime.client.snapshot(active.run_id)
    except rotor.ProcessNotFound:
        await turns.finish(
            chat_id,
            active.turn_id,
            active.run_id,
            "failed",
            "dispatcher process is missing",
        )
        return None
    except Exception:
        log.exception("failed to inspect dispatcher process %s", active.run_id)
        return active
    if snapshot.phase != "terminal":
        return active
    await turns.finish(
        chat_id,
        active.turn_id,
        active.run_id,
        "failed",
        str(snapshot.failure or "dispatcher process terminated"),
    )
    return None


async def start_turn(
    chat_id: str,
    origin: typing.Literal["ui", "channel", "worker", "cron"],
    task_id: str | None = None,
    turn_id: str | None = None,
    actor_user_id: str | None = None,
) -> store.turns.ActiveTurn:
    """Get the chat process and durably enqueue one dispatcher turn."""
    from app import server
    from agent import runtime
    from store import chats, events, turns

    resolved_turn_id = turn_id or f"turn_{uuid.uuid4().hex}"
    history = [
        message.model_dump(mode="json") for message in await server._transcript(chat_id)
    ]
    linked = bool(await chats.bindings(chat_id))
    binding = await events.tail(chat_id, "dispatcher")
    process_key = str((binding or {}).get("key") or "dispatcher")
    handle = await runtime.client.start(
        DurableDispatcher,
        input={"chat_id": chat_id},
        key=process_key,
        scope=chat_id,
    )
    if (await handle.snapshot()).phase == "terminal":
        process_key = f"dispatcher:{uuid.uuid4().hex}"
        await events.append(chat_id, "dispatcher", {"key": process_key})
        handle = await runtime.client.start(
            DurableDispatcher,
            input={"chat_id": chat_id},
            key=process_key,
            scope=chat_id,
        )
    payload = TurnInput(
        chat_id=chat_id,
        turn_id=resolved_turn_id,
        origin=origin,
        history=history,
        linked=linked,
        task_id=task_id,
        actor_user_id=actor_user_id,
    )
    await _claim_turn(payload, handle.id)
    await handle.send(payload, idempotency_key=resolved_turn_id)
    generation = await register_turn(payload, handle.id)
    return turns.ActiveTurn(
        resolved_turn_id,
        handle.id,
        origin,
        task_id,
        generation,
        actor_user_id,
    )
