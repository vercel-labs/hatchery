"""Model tools plus the sandbox/workspace effects behind them.

Ported from agentmesh `agent/tools.py`, plus Hatchery's own tools (sandboxes, fx
subagents, attention, channels). `bash` runs inside keyed `run_bash` children,
`skill_view` in a read-only child, and Hatchery tools in `run_hatchery_tool` children; the
thread runs these serially. Signals, secret requests, delegation, and directed task
messages update durable state inline. The thread merges shared memory before each model
turn; `idle` and delegated `complete` start publication handoffs.
"""

import contextvars
import dataclasses
import json
import re
import typing
from typing import Any

import ai
import httpx
import pydantic
import rotor
import rotor.patterns

from hatchery import config, environment
from hatchery.agent import context
from hatchery.worker import provider
from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git
from hatchery.workspace import repo as workspace_repo
from hatchery.workspace import review as workspace_review

TRANSIENT = (httpx.TransportError, ai.errors.ProviderConnectionError, TimeoutError)
MAX_TOOL_MODEL_BYTES = 32 * 1024
TRUNCATION = "\n[... {omitted} bytes omitted ...]\n"
BUSY_TASK_STATES = ("pending", "running", "attention")


# Agentmesh tools. Their bodies never run in the model loop; the thread executes them.


@ai.tool
async def bash(
    command: str,
    description: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run a Bash command in /workspace/self with a timeout.

    Returns exit_code, stdout, and stderr. Use it to read, edit, and organize files
    in self/ and wiki/, to keep disposable notes in /workspace/scratchpad, to inspect
    collective/, and to work with coding repositories. Clone repositories only under
    /workspace/repos, never under self/ or wiki/.
    Prefer small, focused commands. Always provide a concise `description` so the
    activity UI explains the command's purpose. timeout_seconds defaults to the
    configured setting (normally 300 seconds) and can be raised to at most 480 seconds.
    """
    raise RuntimeError("bash runs inside the sandbox task, not in the model loop")


@ai.tool
async def skill_view(
    name: str, file_path: str | None = None, layer: str | None = None
) -> dict[str, Any]:
    """Load one indexed skill or one of its supporting files.

    Call with the exact skill name from the system prompt before following that
    procedure. With no file_path, returns the complete SKILL.md and a support-file
    inventory. A file_path is relative to that skill's directory. The name resolves
    through the layers (your own skill overrides a team skill of the same name); pass
    layer="team" to read the team version beneath your override, or "runtime" or "self".
    """
    raise RuntimeError("skill loading runs inside the sandbox task")


@ai.tool
async def signal(note: str, delay: str) -> dict[str, Any]:
    """Wake this thread once after a delay with a short note.

    `delay` looks like 90s, 15m, 2h, or 1d. The note is delivered verbatim as
    `Signal: <note>`. Use a workspace schedule for recurring work.
    """
    raise RuntimeError("signals are scheduled by the thread, not in the model loop")


@ai.tool
async def cancel(signal_id: str) -> dict[str, str]:
    """Cancel a pending signal by the ID returned when it was scheduled."""
    raise RuntimeError("cancellations are applied by the thread, not in the model loop")


@ai.tool
async def secret_request(name: str, note: str) -> dict[str, str]:
    """Ask the operator to set one named secret for this agent's API handlers.

    Use an uppercase name such as STRIPE_WEBHOOK_SECRET and explain where the
    operator obtains or enters it. You can use the name in handler environment
    variables but cannot read the value through this tool.
    """
    raise RuntimeError("secret requests are routed by the thread")


@ai.tool
async def delegate(objective: str, repository: str = "", branch: str = "") -> dict[str, str]:
    """Delegate an independent task to another thread of this agent.

    Include `owner/repository` and a pushed branch when the task is coding work. The
    child gets its own sandbox, forks your memory at the end of this turn, and clones
    the repository under /workspace/repos. The returned task_id is the short handle to
    use with every other task tool. Continue without waiting for it.
    """
    raise RuntimeError("delegation is routed by the supervisor")


@ai.tool
async def message_parent(text: str) -> dict[str, str]:
    """Send one message to your direct parent immediately.

    Only delegated threads have this tool. Use it for questions, progress, blockers,
    or partial results. It does not change your assignment status and is never replaced
    by a later message. Normal reply text is for the operator viewing this thread.
    """
    raise RuntimeError("parent messages are routed by the thread and supervisor")


@ai.tool
async def message_task(task_id: str, text: str) -> dict[str, str]:
    """Send new work to one of your direct delegated tasks.

    Use the short task_id returned by `delegate` or shown in a task message. This
    validates the target before returning. A completed child reopens when it accepts
    the new message.
    """
    raise RuntimeError("task messages are routed by the supervisor")


@ai.tool
async def cancel_task(task_id: str, reason: str) -> dict[str, str]:
    """Cancel a direct delegated task by its short task_id while retaining its branch."""
    raise RuntimeError("task cancellation is routed by the supervisor")


@ai.tool
async def archive_task(task_id: str) -> dict[str, str]:
    """Archive a finished direct task and its settled descendant subtree.

    Use the short task_id returned by `delegate`. The subtree must be idle, every
    assignment must be completed or cancelled, and every proposal must be resolved.
    Archiving retains journals and branches and removes the subtree from the active
    list.
    """
    raise RuntimeError("task archival is routed by the supervisor")


@ai.tool
async def task_roster() -> dict[str, Any]:
    """List direct tasks with the short task_ids accepted by all task tools."""
    raise RuntimeError("the task roster is projected by the thread")


@ai.tool
async def review_proposal(
    task_id: str, section: str, decision: str, comment: str = ""
) -> dict[str, str]:
    """Approve or reject one delegated task's workspace or wiki proposal.

    Use the short task_id returned by `delegate`. `decision` is `approve` or `reject`.
    Rejections require a useful comment. An
    approval merges the selected section into your workspace; conflicts leave both
    branches unchanged so the child can reconcile and propose again.
    """
    raise RuntimeError("proposal review is applied by the parent thread")


@ai.tool
async def idle(note: str) -> str:
    """Checkpoint the workspace, release the sandbox, and wait for the next message."""
    return f"idle: {note}"


@ai.tool
async def complete(
    summary: str, result: str, deliverables: list[str] | None = None
) -> dict[str, Any]:
    """Complete your delegated assignment and send its final result to your parent.

    Only delegated threads have this tool. It must be the final tool call. Completion
    checkpoints and consolidates your changes, then sends `summary`, `result`, artifact
    paths or URLs in `deliverables`, and any proposals to your parent. Do not call idle
    afterward. A later operator or parent message reopens the assignment.
    """
    raise RuntimeError("task completion is finalized by the delegated thread")


# Hatchery tools. They run in `run_hatchery_tool` children with the trusted chat context.


@dataclasses.dataclass(frozen=True)
class ToolExecution:
    chat_id: str
    turn_id: str
    actor_user_id: str | None
    operation_key: str


_current_tool: contextvars.ContextVar[ToolExecution] = contextvars.ContextVar(
    "thread_tool_execution"
)


def _execution() -> ToolExecution:
    try:
        return _current_tool.get()
    except LookupError as error:
        raise RuntimeError("thread tool called outside durable execution") from error


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
    """Create an extra persistent sandbox owned by this chat and return its metadata.

    Your own thread sandbox already exists; use this only for separate coding work
    such as fx subagents that need other repositories or a bigger machine.
    ``repos`` selects owner/repo working copies to clone; the first is primary.
    ``setup_script`` runs setup, ``ports`` exposes up to four ports, and ``branch``
    or ``git_sha`` selects the primary repo revision. Use small for research,
    reading, triage, light edits, and focused work. Use big for meaningful tests
    or builds, dev servers, browser/E2E work, monorepos, native compilation, or
    other heavy workloads. Files and processes persist across subagent runs.
    """
    from hatchery.agent import sandbox

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

    This only reads sandbox state. The thread sandbox (title "thread") is listed too
    and can host fx subagents.
    """
    from hatchery.agent import sandbox

    return [
        item.model_dump(exclude={"daemon_token"})
        for item in await sandbox.list_all(_execution().chat_id)
    ]


@ai.tool
async def create_subagent(
    sandbox_id: str,
    task: str,
    model: str = config.ModelConfig().id,
) -> dict[str, typing.Any]:
    """Start a fresh fx subagent chat in ``sandbox_id``.

    ``task`` is the context the new subagent receives. It can inspect and change
    the sandbox's persistent files and processes. The returned subagent/task ID,
    sandbox ID, and state mean the launch was accepted and work has started; they
    are not the completed result.
    """
    from hatchery.agent import sandbox

    execution = _execution()
    created = await sandbox.launch_task(
        execution.chat_id,
        sandbox_id,
        task,
        model,
        actor_user_id=execution.actor_user_id,
        request_id=execution.operation_key,
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
    from hatchery.agent import sandbox
    from hatchery import worker

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
    from hatchery import worker

    return await worker.task_status(_execution().chat_id, subagent_id, after, limit)


@ai.tool
async def require_attention(
    reason: typing.Literal["result_available", "blocked"],
) -> dict[str, str]:
    """Set and return this chat's human-attention reason."""
    from hatchery.store import chats, events

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
    from hatchery.channels import destinations

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
    from hatchery.channels import destinations

    execution = _execution()
    return await destinations.find_people(execution.chat_id, query, execution.actor_user_id)


@ai.tool
async def start_thread(
    provider: typing.Literal["slack", "github"],
    destination: str,
    text: str,
    people: list[str] | None = None,
) -> dict:
    """Send the first notification and link its external thread to this chat."""
    from hatchery.channels import destinations

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


HATCHERY_TOOLS = [
    create_sandbox,
    list_sandboxes,
    create_subagent,
    message_subagent,
    check_subagent,
    require_attention,
    find_channels,
    find_people,
    start_thread,
]
TOOLS = [
    bash,
    skill_view,
    delegate,
    message_parent,
    task_roster,
    message_task,
    cancel_task,
    archive_task,
    review_proposal,
    signal,
    cancel,
    secret_request,
    *HATCHERY_TOOLS,
    idle,
    complete,
]
CONTROL = {idle.name, complete.name}
CHILD_ONLY = {message_parent.name, complete.name}
HATCHERY = {tool.name for tool in HATCHERY_TOOLS}


def by_name(name: str) -> ai.AgentTool:
    tool = next((tool for tool in TOOLS if tool.name == name), None)
    if tool is None:
        raise ValueError(f"unknown tool: {name}")
    return tool


def arguments(call: ai.messages.ToolCallPart) -> dict[str, Any]:
    """Validate a model tool call against the tool's signature."""
    validator = by_name(call.tool_name).validator
    if validator is None:
        return json.loads(call.tool_args)
    try:
        validated = validator.model_validate_json(call.tool_args)
    except pydantic.ValidationError as error:
        raise ValueError(f"invalid {call.tool_name} arguments: {error}") from None
    return {name: getattr(validated, name) for name in type(validated).model_fields}


def bash_timeout(call: ai.messages.ToolCallPart, default: int) -> int:
    """Return the validated timeout selected for one Bash call."""
    requested = arguments(call)["timeout_seconds"]
    timeout = default if requested is None else int(requested)
    if not 1 <= timeout <= config.MAX_COMMAND_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {config.MAX_COMMAND_TIMEOUT_SECONDS}"
        )
    return timeout


def signal_delay(args: dict[str, Any]) -> float:
    """Validate and return the seconds for a one-shot delayed signal."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d)", args["delay"])
    if not match or float(match[1]) <= 0:
        raise ValueError("delay must look like 90s, 15m, 2h, or 1d")
    return float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _clip_text(value: str, limit: int) -> str:
    data = value.encode()
    if len(data) <= limit:
        return value
    marker = TRUNCATION.format(omitted=len(data)).encode()
    if len(marker) >= limit:
        return marker[:limit].decode("utf-8", "ignore")
    room = limit - len(marker)
    head = data[: room * 2 // 3].decode("utf-8", "ignore")
    tail = data[-(room - len(head.encode())) :].decode("utf-8", "ignore")
    omitted = len(data) - len(head.encode()) - len(tail.encode())
    return head + TRUNCATION.format(omitted=omitted) + tail


def _stream_preview(value: dict[str, Any], limit: int) -> dict[str, Any] | None:
    streams = {key: item for key in ("stdout", "stderr") if isinstance(item := value.get(key), str)}
    if not streams:
        return None
    fixed = {**value, **dict.fromkeys(streams, "")}
    if len(_json_bytes(fixed)) >= limit:
        return None

    def candidate(total: int) -> dict[str, Any]:
        budgets = {key: total // len(streams) for key in streams}
        spare = total - sum(min(len(item.encode()), budgets[key]) for key, item in streams.items())
        hungry = [key for key, item in streams.items() if len(item.encode()) > budgets[key]]
        for key in hungry:
            extra = spare // len(hungry)
            budgets[key] += extra
            spare -= extra
        return {**value, **{key: _clip_text(item, budgets[key]) for key, item in streams.items()}}

    low, high, best = 0, limit, fixed
    while low <= high:
        middle = (low + high) // 2
        preview = candidate(middle)
        if len(_json_bytes(preview)) <= limit:
            best, low = preview, middle + 1
        else:
            high = middle - 1
    return best


def model_preview(value: Any, limit: int = MAX_TOOL_MODEL_BYTES) -> Any | None:
    """Return a bounded model projection, or None when the complete value already fits."""
    serialized = _json_bytes(value)
    if len(serialized) <= limit:
        return None
    if isinstance(value, dict) and (stream_preview := _stream_preview(value, limit)) is not None:
        return stream_preview

    text = serialized.decode("utf-8", "replace")
    low, high, best = 0, limit, ""
    while low <= high:
        middle = (low + high) // 2
        text_preview = _clip_text(text, middle)
        if len(_json_bytes(text_preview)) <= limit:
            best, low = text_preview, middle + 1
        else:
            high = middle - 1
    return best


def result(
    call: ai.messages.ToolCallPart, value: Any, *, error: bool = False
) -> ai.messages.ToolResultPart:
    part = ai.messages.ToolResultPart(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        result=value,
        result_kind="error" if error else "json",
    )
    if (preview := model_preview(value)) is not None:
        part.set_model_input(preview)
    return part


# Sandbox <-> workspace. `chat` is the owning chat: {"id", "user_id", "repos"}.


async def acquire(
    owner: str,
    branch: str,
    name: str,
    chat: dict[str, Any],
    *,
    upstream: str = "main",
    upstream_sha: str = "",
) -> provider.Sandbox:
    """Reacquire the thread sandbox; only a fresh one is seeded from the Git branch."""
    env = environment.Environment.current()

    async def seed() -> workspace_files.Tree:
        return await env.workspaces.materialize(
            owner, branch, upstream=upstream, upstream_sha=upstream_sha
        )

    thread_chat = provider.ThreadChat(
        id=chat["id"], user_id=chat.get("user_id"), repos=tuple(chat.get("repos") or ())
    )
    acquired = await env.sandboxes.acquire(
        name, seed=seed, owner=owner, purpose="thread", chat=thread_chat
    )
    return acquired.sandbox


async def _sandbox_changed(chat_id: str) -> None:
    from hatchery.store import events

    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


async def checkpoint(
    owner: str,
    branch: str,
    name: str,
    chat: dict[str, Any],
    *,
    process_id: str,
    operation_id: str,
    release: bool,
    acquired: provider.Sandbox | None = None,
    upstream: str = "main",
) -> str:
    """Push self/ and wiki/ to the thread branch, then optionally stop the sandbox."""
    env = environment.Environment.current()
    sandbox = acquired or await acquire(owner, branch, name, chat, upstream=upstream)
    files = await sandbox.download(["self", "wiki"])
    sha = await env.workspaces.checkpoint(
        owner, branch, files, process_id=process_id, operation_id=operation_id
    )
    if release:
        await env.sandboxes.release(sandbox, keep=True)
        await _sandbox_changed(chat["id"])
    return sha


async def busy(chat_id: str, name: str) -> bool:
    """Whether fx subagents or open terminals still use this chat sandbox."""
    from hatchery.worker import store as worker_store

    worker_id = name.removeprefix("hatchery-")
    tasks = await worker_store.list_tasks(chat_id)
    if any(task.worker_id == worker_id and task.status in BUSY_TASK_STATES for task in tasks):
        return True
    terminals = await worker_store.list_terminals(chat_id)
    return any(
        terminal.worker_id == worker_id and terminal.status != "exited" for terminal in terminals
    )


async def release(
    owner: str, branch: str, name: str, chat: dict[str, Any], *, upstream: str = "main"
) -> bool:
    """Stop an already-checkpointed sandbox after its idle grace; False if still in use."""
    if await busy(chat["id"], name):
        return False
    env = environment.Environment.current()
    sandbox = await acquire(owner, branch, name, chat, upstream=upstream)
    await env.sandboxes.release(sandbox, keep=True)
    await _sandbox_changed(chat["id"])
    return True


async def refresh(
    owner: str,
    branch: str,
    name: str,
    chat: dict[str, Any],
    *,
    head: str,
    base: str,
    process_id: str,
    acquired: provider.Sandbox | None = None,
    upstream: str = "main",
) -> workspace_repo.Refresh:
    """Merge upstream into the checkpointed thread branch and install the result."""
    env = environment.Environment.current()
    refreshed = await env.workspaces.refresh(
        owner, branch, head=head, base=base, process_id=process_id, upstream=upstream
    )
    if refreshed.sha != head:
        sandbox = acquired or await acquire(owner, branch, name, chat, upstream=upstream)
        await sandbox.replace(["self", "wiki"], refreshed.files)
    return refreshed


# Durable tasks


@rotor.patterns.task
async def run_bash(
    owner: str,
    branch: str,
    sandbox: str,
    chat: dict[str, Any],
    call: dict[str, Any],
    upstream: str = "main",
) -> dict[str, Any]:
    """One sandbox command, one durable child. Returns a ToolResultPart as JSON."""
    part = ai.messages.ToolCallPart.model_validate(call)
    try:
        args = arguments(part)
        timeout = bash_timeout(
            part, environment.Environment.current().config.thread.command_timeout_seconds
        )
    except ValueError as error:
        return result(part, str(error), error=True).model_dump(mode="json")
    command = args["command"]
    box = await acquire(owner, branch, sandbox, chat, upstream=upstream)
    try:
        outcome = await box.exec(command, timeout=timeout)
    except provider.SandboxCommandStopped as error:
        message = str(error)
        rotor.record("bash_recovered", {"sandbox": sandbox, "command": command, "error": message})
        return result(part, message, error=True).model_dump(mode="json")
    output: dict[str, Any] = {
        "exit_code": outcome.exit_code,
        "stdout": outcome.stdout,
        "stderr": outcome.stderr,
    }
    rotor.record(
        part.tool_name,
        {"sandbox": sandbox, "command": command, "timeout_seconds": timeout, **output},
    )
    return result(part, output).model_dump(mode="json")


# Maximum command (480s) + dependency sync (150s) + remote and acquisition headroom.
run_bash.handle_timeout = "720s"


@rotor.patterns.task
async def run_skill_view(
    owner: str,
    branch: str,
    sandbox: str,
    chat: dict[str, Any],
    call: dict[str, Any],
    upstream: str = "main",
) -> dict[str, Any]:
    """Resolve one skill view from the thread's current workspace tree."""
    part = ai.messages.ToolCallPart.model_validate(call)
    try:
        args = arguments(part)
    except ValueError as error:
        return result(part, str(error), error=True).model_dump(mode="json")
    box = await acquire(owner, branch, sandbox, chat, upstream=upstream)
    tree = await box.download(["self/skills", "wiki/skills"])
    try:
        output = context.view_skill(
            context.layered(tree), str(args["name"]), args["file_path"], args["layer"]
        )
    except ValueError as error:
        return result(part, str(error), error=True).model_dump(mode="json")
    if model_preview(output) is not None:
        message = "skill view exceeds the model-input limit; split it into smaller support files"
        return result(part, message, error=True).model_dump(mode="json")
    rotor.record(
        skill_view.name,
        {
            "sandbox": sandbox,
            "name": args["name"],
            "file": args["file_path"],
            "layer": output["layer"],
        },
    )
    return result(part, output).model_dump(mode="json")


run_skill_view.handle_timeout = "300s"


@rotor.patterns.task
async def run_hatchery_tool(
    chat_id: str,
    turn_id: str,
    actor_user_id: str | None,
    call: dict[str, Any],
) -> dict[str, Any]:
    """Validate and execute one Hatchery tool call with the trusted chat context."""
    from hatchery.agent import telemetry

    part = ai.messages.ToolCallPart.model_validate(call)
    if part.tool_name not in HATCHERY:
        return result(part, f"unknown tool: {part.tool_name}", error=True).model_dump(mode="json")
    try:
        args = arguments(part)
    except (ValueError, json.JSONDecodeError) as error:
        return result(part, str(error), error=True).model_dump(mode="json")
    execution = ToolExecution(
        chat_id=chat_id,
        turn_id=turn_id,
        actor_user_id=actor_user_id,
        operation_key=rotor.idempotency_key(part.tool_call_id),
    )
    token = _current_tool.set(execution)
    try:
        async with telemetry.use_chat(chat_id):
            async with ai.experimental_telemetry.span("hatchery.thread.tool") as span:
                span.set_attrs(
                    {
                        "chat.id": chat_id,
                        "turn.id": turn_id,
                        "gen_ai.tool.call.id": part.tool_call_id,
                    },
                    tool_name=part.tool_name,
                )
                try:
                    value = await by_name(part.tool_name).fn(**args)
                except Exception as error:
                    span.set_attrs(tool_error=True)
                    return result(part, str(error), error=True).model_dump(mode="json")
                return result(part, value).model_dump(mode="json")
    finally:
        _current_tool.reset(token)
        telemetry.flush()


def _retry_consolidation(error: Exception, attempt: int) -> str | None:
    # A refused review is a decision, not a fault; anything else gets two more tries.
    return None if isinstance(error, workspace_review.ReviewRequired) or attempt >= 2 else "5s"


@rotor.patterns.task(retry=_retry_consolidation)
async def consolidate(
    owner: str,
    branch: str,
    section: str,
    head: str,
    summary: str,
    policy: str,
    process_id: str,
    upstream: str = "main",
) -> dict[str, Any]:
    """Merge one section of a thread checkpoint into upstream and hand its proposal to review.

    Git merges clean work. Conflicted paths return to the thread, which refreshes,
    repairs the markers, and idles again.
    """
    env = environment.Environment.current()
    headline = " ".join(summary.split())[:200] or "Consolidate thread memory"
    outcome = await env.workspaces.consolidate(
        owner,
        branch,
        section,
        head=head,
        process_id=process_id,
        summary=headline,
        upstream=upstream,
    )
    output: dict[str, Any] = {"section": section, "head": head}
    if outcome.conflicts:
        return {**output, "conflicts": list(outcome.conflicts)}
    if outcome.obsolete:
        if policy == "parent":
            await env.workspaces.delete_proposal(outcome.obsolete, upstream=upstream)
        else:
            await env.review.withdraw(outcome.obsolete)
        return {**output, "withdrawn": outcome.obsolete}
    if not outcome.proposal:
        return output
    inspected = await env.workspaces.inspect_proposal(outcome.proposal)
    if policy == "parent":
        parent_proposal = {
            "section": section,
            "branch": outcome.proposal,
            "sha": inspected.sha,
            "thread_id": process_id,
            "url": "",
            "merged": False,
            "reviewer": "parent",
        }
        rotor.record("proposal_published", parent_proposal)
        return {**output, "proposal": parent_proposal}
    if section == "workspace":
        if any(workspace_repo.is_serve_path(owner, path) for path in inspected.changes):
            policy = env.config.review.serve
    review = await env.review.submit(
        branch=outcome.proposal, summary=headline, owner=owner, section=section, policy=policy
    )
    rotor.record("proposal_published", {"section": section, **dataclasses.asdict(review)})
    return {
        **output,
        "proposal": {
            "section": section,
            "branch": outcome.proposal,
            "sha": inspected.sha,
            "thread_id": process_id,
            "url": review.url,
            "merged": review.merged,
        },
    }


consolidate.handle_timeout = "300s"


def _retry_review(error: Exception, attempt: int) -> str | None:
    if isinstance(error, (workspace_git.MergeConflict, ValueError)) or attempt >= 2:
        return None
    return "2s" if isinstance(error, workspace_git.MainChanged) else "5s"


@rotor.patterns.task(retry=_retry_review)
async def run_review(
    owner: str,
    branch: str,
    sandbox: str,
    chat: dict[str, Any],
    upstream: str,
    call: dict[str, Any],
    proposal: str,
    proposal_sha: str,
    child_process_id: str,
    head: str,
    task_id: str,
) -> dict[str, Any]:
    """Merge one child proposal into its parent's branch and live sandbox."""
    part = ai.messages.ToolCallPart.model_validate(call)
    try:
        accepted = await environment.Environment.current().workspaces.accept(
            owner,
            branch,
            proposal,
            head=head,
            proposal_sha=proposal_sha,
            child_process_id=child_process_id,
            task_id=task_id,
        )
    except (workspace_git.MergeConflict, ValueError) as error:
        return {
            "result": result(part, str(error), error=True).model_dump(mode="json"),
            "error": str(error),
        }
    sandbox_synced = True
    try:
        box = await acquire(owner, branch, sandbox, chat, upstream=upstream)
        await box.replace(["self", "wiki"], accepted.files)
    except Exception as error:
        sandbox_synced = False
        rotor.record(
            "proposal_sandbox_sync_deferred", {"sandbox": sandbox, "error": str(error)[:500]}
        )
    value = {
        "status": "approved",
        "task_id": task_id,
        "branch": proposal,
        "proposal_sha": proposal_sha,
        "merge_sha": accepted.sha,
        "changed": list(accepted.changed),
        "sandbox_synced": sandbox_synced,
    }
    return {"result": result(part, value).model_dump(mode="json"), **value}


run_review.handle_timeout = "300s"


def _retry_cleanup(error: Exception, attempt: int) -> str | None:
    return f"{2**attempt}s" if attempt < 4 else None


@rotor.patterns.task(retry=_retry_cleanup)
async def cleanup_sandbox(
    owner: str,
    branch: str,
    sandbox: str,
    chat: dict[str, Any],
    process_id: str,
    operation_id: str,
    upstream: str = "main",
    checkpoint_sha: str = "",
) -> str:
    """Final push and stop for a thread that died; its sandbox is retained for recovery."""
    env = environment.Environment.current()
    box = await acquire(owner, branch, sandbox, chat, upstream=upstream)
    try:
        head = await env.workspaces.thread_head(owner, branch)
        if checkpoint_sha and head != checkpoint_sha:
            files = await env.workspaces.materialize(owner, branch, upstream=upstream)
            await box.replace(["self", "wiki"], files)
            await env.sandboxes.release(box, keep=True)
            return head
        return await checkpoint(
            owner,
            branch,
            sandbox,
            chat,
            process_id=process_id,
            operation_id=operation_id,
            release=True,
            acquired=box,
            upstream=upstream,
        )
    except Exception:
        await env.sandboxes.release(box, keep=True)
        raise
