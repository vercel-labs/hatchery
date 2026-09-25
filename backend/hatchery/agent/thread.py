"""One durable model loop with a sandbox, a Git branch, and a chat.

Ported from agentmesh `agent/thread.py` (`AgentThread`) plus Hatchery's chat projection,
channel delivery, and tools. A thread is the only runtime: the durable AI SDK loop on Rotor.

`Step` is the sole dispatcher. It runs the next queued tool call, applies the turn's
trailing idle decision, or asks the supervisor for budget admission; `Admitted` runs
one model call. Waiting is an empty mailbox: an idle thread and a thread held for
budget both hold no lease and run no code.

Status is derived from state, never stored: `error` means parked, `archived` means
shelved, a publication child means handing off, queued work or an owed model call means
active, and nothing means idle. Publication runs in a keyed child while the thread keeps
accepting input.

Every thread has a chat. A root thread is the user's chat; a delegated thread gets its
own chat (trigger "task", linked to the parent chat). The chat's `messages` stream is
the complete operator transcript; the thread's `messages` are the model history, which
compaction may shorten. Hatchery turns (`TurnInput`) map onto thread inputs: each turn
streams `{kind: "agent", turn_id, event}` to Rotor's live spool, and when its answer is
final `finish_turn` persists the new messages, delivers replies to linked channels, and
projects the terminal turn. Work the thread starts by itself (signals, task messages,
resumes) runs under an internal turn registered the same way.
"""

import collections
import dataclasses
import hashlib
import logging
import typing
from typing import Any, Literal

import ai
import rotor

from hatchery import environment, messages, model_budget, provider_errors, vault
from hatchery.agent import compaction, context, tools
from hatchery.worker import provider
from hatchery.workspace import repo as workspace_repo

Status = Literal["active", "handing_off", "idle", "archived", "parked"]
TURN_CEILING = "turn ceiling reached"
BUDGET_HELD = "daily token budget exhausted; waiting for a grant or the next UTC day"
MODEL_RETRIES = 2
SANDBOX_RELEASE_KEY = "sandbox-release"
CONTEXT_CACHE_LIMIT = 128
SEEN_INPUTS = 256
TASK_STATUSES = ("working", "completed", "cancelled")
PARENT_MESSAGE_HEADER = "Message from parent thread:"
log = logging.getLogger(__name__)
_CONTEXT_CACHE: collections.OrderedDict[tuple[str, str], context.Context] = (
    collections.OrderedDict()
)


def child_chat_id(thread_id: str) -> str:
    """The deterministic chat record of a delegated thread."""
    return "chat_" + hashlib.sha256(thread_id.encode()).hexdigest()[:12]


def _model_message(raw: dict[str, Any]) -> ai.messages.Message:
    message = ai.messages.Message.model_validate(raw)
    if raw.get("source") == "parent" and message.role == "user":
        return ai.user_message(f"{PARENT_MESSAGE_HEADER}\nMessage:\n{message.text}")
    return message


def _affects_context(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in context.CONTEXT_PATHS)


def _proposal_key(proposal: dict[str, Any]) -> tuple[Any, Any]:
    return proposal.get("branch"), proposal.get("sha")


def _mark_merged(proposals: list[dict[str, Any]], key: tuple[Any, Any], merge_sha: str) -> None:
    for proposal in proposals:
        if _proposal_key(proposal) == key:
            proposal["merged"] = True
            proposal["merge_sha"] = merge_sha


@rotor.state
class Idle:
    """The turn's trailing decision: the idle call to answer, if any, and its note."""

    note: str = ""
    call: dict[str, Any] | None = None


@rotor.state
class Completion:
    """A final delegated result waiting for its publication handoff to finish."""

    summary: str = ""
    result: str = ""
    deliverables: list[str] = dataclasses.field(default_factory=list)
    call: dict[str, Any] | None = None


@rotor.state
class Compaction:
    """One summary call: why it is needed, how much recent history to keep, and the
    input estimate that triggered it."""

    reason: str = ""  # threshold | fit | <provider failure>
    keep: int = 0
    estimate: int = 0

    @property
    def provider_rejected(self) -> bool:
        return self.reason not in ("threshold", "fit")


@rotor.state
class ChildTask:
    """This thread's view of one task it delegated."""

    task_id: str = ""
    handle: str = ""  # task-N, what the model sees and uses
    thread_id: str = ""
    chat_id: str = ""
    objective: str = ""
    status: str = "working"
    summary: str = ""
    result: str = ""
    deliverables: list[str] = dataclasses.field(default_factory=list)
    archived: bool = False
    proposals: list[dict[str, Any]] = dataclasses.field(default_factory=list)


@rotor.state
class ThreadState:
    owner: str = ""  # the agent ID
    chat_id: str = ""
    actor_user_id: str = ""  # whose GitHub connection the sandbox uses
    repos: list[str] = dataclasses.field(default_factory=list)
    branch: str = ""
    sandbox: str = ""
    base_sha: str = ""  # the upstream commit the thread branch forked from
    upstream: str = "main"
    upstream_sha: str = ""
    parent_thread_id: str = ""
    depth: int = 0
    objective: str = ""
    # Model history, without the system message (rebuilt from the workspace each turn).
    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    pending: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # next-turn inputs
    # The turn in flight.
    calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # tool calls left
    running: dict[str, Any] | None = None  # the tool call whose child is live
    results: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    control: Idle | Completion | None = None
    completion: Completion | None = None  # final control whose consolidation is in flight
    # The model call we owe: the epoch of its Admit, or a stale epoch after an activation
    # (Step re-asks). None once the call ran. A queued compaction runs first.
    admission: int | None = None
    held_epoch: int | None = None  # admission the supervisor held for exhausted budget
    compaction: Compaction | None = None
    last_compaction: Compaction | None = None  # finished this turn attempt, if any
    # Publication.
    consolidating: list[str] = dataclasses.field(default_factory=list)  # sections left
    handoffs: int = 0  # keys consolidation children uniquely
    proposals: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # one per section
    updated: list[str] = dataclasses.field(default_factory=list)  # relevant clean refreshes
    conflicts: list[str] = dataclasses.field(default_factory=list)  # markers left to repair
    core_context_revisions: dict[str, str] = dataclasses.field(default_factory=dict)
    core_updated: list[str] = dataclasses.field(default_factory=list)
    # Steering.
    archived: bool = False
    task_id: str = ""
    task_handle: str = ""
    task_status: str = "working"
    task_cancelled: bool = False
    completion_summary: str = ""
    completion_result: str = ""
    deliverables: list[str] = dataclasses.field(default_factory=list)
    delegations: list[dict[str, str]] = dataclasses.field(default_factory=list)
    tasks: dict[str, ChildTask] = dataclasses.field(default_factory=dict)
    task_sequence: int = 0
    interrupt_after_tool: str = ""  # a stop deferred until the running review settles
    signals: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    dirty: bool = False  # a tool ran since the last checkpoint
    sandbox_live: bool = False
    sandbox_needs_sync: bool = False
    turns: int = 0
    ceiling: int = 0
    epoch: int = 0  # advanced per admission request, activation, and stop; fences replies
    quiet_until: float = 0  # after an uncertain bash child, wait out its remote timeout
    # Hatchery chat projection.
    linked: bool = False  # the chat has Slack/GitHub bindings; start_thread is hidden
    turn: dict[str, Any] | None = None  # the Hatchery turn streaming now
    turn_queue: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    turn_sequence: int = 0  # numbers internal turns
    replies: list[str] = dataclasses.field(default_factory=list)  # current turn's texts
    unprojected: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    unstreamed: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    finishing: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    seen_inputs: list[str] = dataclasses.field(default_factory=list)
    # Reporting.
    input_tokens: int = 0
    output_tokens: int = 0
    last_input_tokens: int = 0
    compactions: int = 0
    summary: str = ""
    result: str = ""
    error: str = ""
    checkpoint_sha: str = ""
    context_cache_sha: str = ""


# Chat projection tasks


async def _persist_message(chat_id: str, message: dict[str, Any]) -> str:
    from hatchery.app import server
    from hatchery.store import events

    parsed = ai.messages.Message.model_validate(message)
    if all(item.id != parsed.id for item in await server._transcript(chat_id)):
        await events.append(chat_id, "messages", message)
    return parsed.id


async def register_turn(turn: messages.TurnInput, process_id: str) -> int:
    """Project one accepted turn into Hatchery's UI and cron stores."""
    from hatchery.store import events, jobs, turns

    async with turns.run(turn.chat_id):
        records = await events.read(turn.chat_id, "turns")
        existing = next(
            (
                (index, data)
                for index, data in records
                if data.get("type") == "turn.started" and data.get("turn_id") == turn.turn_id
            ),
            None,
        )
        if existing is not None and existing[1].get("run_id") != process_id:
            raise RuntimeError(
                f"turn {turn.turn_id} is already owned by {existing[1].get('run_id')}"
            )
        if turn.origin == "cron" and not await jobs.claim_run(turn.turn_id, process_id):
            owner = await jobs.started_run(turn.turn_id)
            if owner != process_id:
                raise RuntimeError(f"scheduled turn {turn.turn_id} is already owned by {owner}")
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
            data.get("type") == "stream.available" and data.get("turn_id") == turn.turn_id
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


@rotor.patterns.task
async def register_thread_turn(chat_id: str, turn_id: str, process_id: str) -> int:
    """Register a turn the thread started by itself (signal, task message, resume)."""
    return await register_turn(
        messages.TurnInput(chat_id=chat_id, turn_id=turn_id, origin="thread"), process_id
    )


@rotor.patterns.task
async def announce_thread_turn(chat_id: str) -> None:
    """Emit channel thinking state when a turn starts streaming."""
    from hatchery.app import server
    from hatchery import channels

    await server._emit(chat_id, channels.event(channels.protocol.TURN_STARTED))


def _retry_finish(error: Exception, attempt: int) -> str | None:
    return f"{2**attempt}s" if attempt < 4 else None


@rotor.patterns.task(retry=_retry_finish)
async def finish_turn(
    chat_id: str,
    turn_id: str,
    process_id: str,
    state: Literal["completed", "failed", "cancelled"],
    task_id: str | None = None,
    error: str | None = None,
    replies: list[str] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Persist a turn's new messages, deliver its replies, and project its end."""
    from hatchery.app import server
    from hatchery.store import chats, events, turns
    from hatchery import channels, worker

    replies = list(replies or [])
    for message in messages or []:
        await _persist_message(chat_id, message)
    if state == "completed":
        for index, reply in enumerate(replies):
            failures = await server._deliver(
                chat_id,
                reply,
                final=index == len(replies) - 1,
                delivery_key=f"{turn_id}:{index}",
            )
            if failures:
                raise RuntimeError("; ".join(failures))
    elif state == "failed":
        await server._emit(
            chat_id,
            channels.event(channels.protocol.TURN_FAILED, error=error),
            delivery_key=f"{turn_id}:failure",
        )
    if task_id is not None:
        task = await worker.get_task(chat_id, task_id)
        if task is not None:
            final = replies[-1] if replies else (error or "")

            def mark_delivered(current: worker.Task) -> worker.Task:
                current.completion_message = final
                current.completion_delivered = True
                return current

            task = await worker.store.mutate_task(task.id, mark_delivered)
            if task is not None and task.status in ("complete", "errored"):
                siblings = await worker.store.list_tasks(chat_id)
                if not any(
                    sibling.id != task.id and sibling.status in ("pending", "running", "attention")
                    for sibling in siblings
                ):
                    await chats.finish(
                        chat_id, "failed" if task.status == "errored" else "done", final
                    )
            await events.append(chat_id, "ui", {"type": "chat.changed"})
    await turns.finish(chat_id, turn_id, process_id, state, error)
    await events.append(chat_id, "ui", {"type": "messages.changed"})
    return state


class AgentThread(rotor.DurableProcess[ThreadState]):
    spool = True
    handle_timeout = "300s"

    @property
    def _agent(self) -> rotor.ProcessRef:
        assert self.parent is not None, "threads are spawned by a supervisor"
        return self.parent

    def _chat(self) -> dict[str, Any]:
        s = self.state
        return {"id": s.chat_id, "user_id": s.actor_user_id or None, "repos": list(s.repos)}

    # Lifecycle and steering

    @rotor.on
    async def start(self, msg: rotor.Start) -> None:
        from hatchery.store import chats, events

        if self.parent is None:
            raise ValueError("threads are spawned by a supervisor")
        s, env = self.state, environment.Environment.current()
        s.owner = msg.input["owner"]
        s.branch = env.workspaces.thread_branch(s.owner, self.ref.id)
        s.sandbox = provider.sandbox_name(self.ref.id)
        s.task_id = str(msg.input.get("task_id", self.ref.id))
        s.task_handle = str(msg.input.get("task_handle", ""))
        s.upstream = str(msg.input.get("upstream", "main"))
        s.upstream_sha = str(msg.input.get("upstream_sha", ""))
        s.parent_thread_id = str(msg.input.get("parent_thread_id", ""))
        s.depth = int(msg.input.get("depth", 0))
        s.objective = str(msg.input.get("objective", ""))
        s.actor_user_id = str(msg.input.get("actor_user_id") or "")
        s.ceiling = env.config.thread.max_turns
        s.chat_id = str(msg.input.get("chat_id") or child_chat_id(self.ref.id))
        if s.parent_thread_id:
            await chats.create_once(
                s.chat_id,
                s.owner,
                (s.objective.strip().splitlines() or ["task"])[0][:80],
                None,
                trigger="task",
                parent_chat_id=str(msg.input.get("parent_chat_id") or "") or None,
            )
            if await events.tail(s.chat_id, "thread") is None:
                await events.append(
                    s.chat_id, "thread", {"agent_id": s.owner, "thread_id": self.ref.id, "cursor": -1}
                )
            await events.append(s.chat_id, "ui", {"type": "chat.changed"})
        for prompt in msg.input.get("prompts", []):
            self._buffer(messages.Prompt(**prompt))
        for turn in msg.input.get("turns", []):
            self._accept_turn(messages.TurnInput(**turn))
        self.send(self.ref, messages.Step())
        self._report()

    def _buffer(self, msg: messages.Prompt) -> None:
        if msg.text.strip():
            self._queue_input(msg.text, msg.source, request_id=msg.request_id)

    def _reopen_assignment(self) -> None:
        """A new operator or parent prompt makes a completed child active again."""
        s = self.state
        if s.parent_thread_id and s.task_status != "working" and s.task_status != "cancelled":
            s.task_status = "working"
            s.completion_summary = ""
            s.completion_result = ""
            s.deliverables.clear()

    def _queue_input(self, text: str, source: str, *, request_id: str = "", **meta: Any) -> None:
        self.state.pending.append({"text": text, "request_id": request_id, "source": source, **meta})

    def _wake(self, *, unless: typing.Collection[str] = ()) -> None:
        """Take a turn for newly queued input, except while resting in an `unless` status."""
        status = self._status()
        if status == "active":
            self.send(self.ref, messages.Step())
            self._report()
        elif status in unless:
            self._report()
        else:
            self._activate()

    def _activate(self) -> None:
        """Leave a resting state and take a turn; stale admissions are fenced."""
        s = self.state
        self.unschedule(SANDBOX_RELEASE_KEY)
        s.epoch += 1
        s.error = ""
        s.compaction = None
        s.last_compaction = None
        self.send(self.ref, messages.Step())
        self._report()

    def _accept_turn(self, msg: messages.TurnInput) -> bool:
        """Queue a Hatchery turn's new chat messages; False when it brought nothing new."""
        s = self.state
        if msg.chat_id != s.chat_id:
            raise ValueError("turn belongs to another chat")
        if msg.actor_user_id:
            s.actor_user_id = msg.actor_user_id
        s.linked = s.linked or msg.linked
        accepted = 0
        for raw in msg.messages:
            message = ai.messages.Message.model_validate(raw)
            if message.id in s.seen_inputs or not message.text.strip():
                continue
            s.seen_inputs = [*s.seen_inputs, message.id][-SEEN_INPUTS:]
            self._queue_input(message.text, msg.origin, request_id=msg.turn_id, message=raw)
            accepted += 1
        turn = {
            "turn_id": msg.turn_id,
            "origin": msg.origin,
            "task_id": msg.task_id,
            "actor_user_id": msg.actor_user_id,
        }
        if not accepted:
            # Nothing new to answer (for example a replayed ingress): end it right away.
            self._spawn_finish(turn, "completed")
            return False
        s.turn_queue.append(turn)
        return True

    @rotor.on
    async def request_turn(self, msg: messages.TurnInput) -> None:
        s = self.state
        if not self._accept_turn(msg):
            return
        if s.parent_thread_id:
            if s.completion is not None and not s.consolidating:
                s.completion = None
            if s.completion is None:
                self._reopen_assignment()
        if self._status() == "parked":
            # The input waits for an operator resume; the turn itself ends now.
            self._fail_turns(f"thread is parked: {s.error}; resume it to continue")
        self._wake(unless=("handing_off", "parked"))

    @rotor.on
    async def prompt(self, msg: messages.Prompt) -> None:
        self._buffer(msg)
        s = self.state
        if s.parent_thread_id and msg.source != "maintenance":
            # A healthy/parked handoff must finish before new work can supersede its
            # completion. A completion interrupted during conflict repair is abandoned.
            if s.completion is not None and not s.consolidating:
                s.completion = None
            if s.completion is None:
                self._reopen_assignment()
        self._wake(unless=("handing_off", "parked"))

    @rotor.on
    async def receive_signal(self, msg: messages.Signal) -> None:
        if self.state.signals.pop(msg.id, None) is None:
            return
        self._queue_input(f"Signal: {msg.note}", "signal")
        rotor.record("signal_fired", {"id": msg.id, "note": msg.note})
        self._wake(unless=("archived",))

    def _queue_task_input(self, text: str, *, task_id: str, task_status: str, **meta: str) -> None:
        """Coordination data from the supervisor; it wakes an idle thread but not a resting one."""
        self._queue_input(text, "task", task_id=task_id, task_status=task_status, **meta)
        rotor.record("task_input", {"task_id": task_id, "status": task_status})
        self._wake(unless=("archived", "parked", "handing_off"))

    @rotor.on
    async def receive_task_message(self, msg: messages.TaskMessageInput) -> None:
        if self.state.task_cancelled:
            return
        task = self.state.tasks.setdefault(msg.task_id, ChildTask(task_id=msg.task_id))
        task.handle = msg.task_handle or task.handle or msg.task_id
        task.thread_id = msg.reporting_thread_id
        task.objective = msg.objective
        task.status = msg.status if msg.status in TASK_STATUSES else "working"
        task.archived = False
        self._queue_task_input(
            "Message from delegated task:\n"
            f"Task: {task.handle}\n"
            f"Reply target: use `{task.handle}` with the task tools.\n"
            f"Objective: {msg.objective}\n"
            f"Message:\n{msg.text}",
            task_id=msg.task_id,
            task_status=task.status,
            reporting_thread_id=msg.reporting_thread_id,
            task_handle=task.handle,
            task_event="message",
        )

    @rotor.on
    async def receive_task_completion(self, msg: messages.TaskCompletionInput) -> None:
        if self.state.task_cancelled:
            return
        task = self.state.tasks.setdefault(msg.task_id, ChildTask(task_id=msg.task_id))
        task.handle = msg.task_handle or task.handle or msg.task_id
        task.thread_id = msg.reporting_thread_id
        task.objective = msg.objective
        task.status = msg.status if msg.status in TASK_STATUSES else "working"
        task.summary = msg.summary if task.status == "completed" else ""
        task.result = msg.result if task.status == "completed" else ""
        task.deliverables = list(msg.deliverables) if task.status == "completed" else []
        task.archived = False
        merged = {_proposal_key(p): p for p in task.proposals if p.get("merged")}
        task.proposals = [{**p, **merged.get(_proposal_key(p), {})} for p in msg.proposals]
        lines = [
            "Delegated task completed:",
            f"Task: {task.handle}",
            f"Reply target: use `{task.handle}` with the task tools.",
            f"Objective: {msg.objective}",
            f"Summary: {msg.summary}",
            f"Result:\n{msg.result}",
        ]
        if msg.deliverables:
            lines.append("Deliverables:\n" + "\n".join(f"- {item}" for item in msg.deliverables))
        if msg.proposals:
            lines.append(
                "Proposals:\n"
                + "\n".join(
                    f"- {p.get('section', '')}: {p.get('branch', '')}" for p in msg.proposals
                )
            )
        if msg.roster:
            lines.append(
                "Child roster:\n"
                + "\n".join(
                    f"- {item.get('task_id', '')}: {item.get('status', '')} \N{EM DASH} "
                    f"{item.get('objective', '')}"
                    for item in msg.roster
                )
            )
        self._queue_task_input(
            "\n".join(lines),
            task_id=msg.task_id,
            task_status=task.status,
            reporting_thread_id=msg.reporting_thread_id,
            task_handle=task.handle,
            task_event="completion",
        )

    @rotor.on
    async def task_action_accepted(self, msg: messages.TaskActionAccepted) -> None:
        task = self.state.tasks.get(msg.task_id)
        if task is not None:
            task.status = msg.status
            task.archived = task.archived or msg.action == "archive"
        rotor.record(
            "task_action_accepted",
            {"task_id": msg.task_id, "action": msg.action, "status": msg.status},
        )
        if msg.action == "archive":
            self._queue_task_input(
                f"Task archived:\nTask ID: {self._task_ref(msg.task_id)}\n"
                f"Archived threads: {len(msg.thread_ids)}",
                task_id=msg.task_id,
                task_status=msg.status,
            )
        else:
            self._report()

    @rotor.on
    async def task_action_rejected(self, msg: messages.TaskActionRejected) -> None:
        self._queue_task_input(
            f"Task action rejected:\nTask ID: {self._task_ref(msg.task_id)}\n"
            f"Action: {msg.action}\nCode: {msg.code}\nReason: {msg.reason}\n"
            f"Valid direct task IDs: {self._valid_handles()}",
            task_id=msg.task_id,
            task_status="rejected",
        )

    @rotor.on
    async def delegation_rejected(self, msg: messages.DelegationRejected) -> None:
        task = self.state.tasks.pop(msg.task_id, None)
        handle = task.handle if task is not None else msg.task_id
        self._queue_task_input(
            f"Delegation rejected:\nTask: {handle}\n"
            f"Objective: {msg.objective}\nReason: {msg.reason}",
            task_id=msg.task_id,
            task_status="rejected",
        )

    @rotor.on
    async def resume(self, msg: messages.Resume) -> None:
        s = self.state
        if self._status() != "parked":
            return
        if s.consolidating:
            s.error = ""
            self._consolidate_next()
            self._report()
            return
        if s.error == TURN_CEILING:
            s.ceiling += environment.Environment.current().config.thread.max_turns
            rotor.record("turn_ceiling_extended", {"ceiling": s.ceiling})
        self._activate()

    @rotor.on
    async def stop(self, msg: messages.Stop) -> None:
        """Interrupt the current activation and return this live thread to idle."""
        await self._stop(msg.reason)

    @rotor.on
    async def cancel_assigned_task(self, msg: messages.CancelAssignedTask) -> None:
        self.state.task_cancelled = True
        self.state.task_status = "cancelled"
        self._cancel_all_signals()
        await self._stop(msg.reason)

    async def _stop(self, reason: str) -> None:
        s = self.state
        if s.running is not None and s.running["tool_name"] == tools.review_proposal.name:
            # Approval must settle Git and sandbox together; interrupt right after it.
            s.interrupt_after_tool = reason
            rotor.record(
                "thread_stop_deferred", {"reason": reason, "tool": tools.review_proposal.name}
            )
            self._report()
            return
        s.epoch += 1
        for key in ("model-retry", "quiescence", SANDBOX_RELEASE_KEY):
            self.unschedule(key)
        if s.running is not None:
            self.cancel(rotor.child_id(self.ref.id, self._tool_key(s.running)))
        if s.consolidating:
            self.cancel(rotor.child_id(self.ref.id, self._consolidation_key()))
        interrupted = [*([s.running] if s.running else []), *s.calls]
        if s.control is not None and s.control.call is not None:
            interrupted.append(s.control.call)
        for call in interrupted:
            self._result(
                ai.messages.ToolCallPart.model_validate(call), f"interrupted: {reason}", error=True
            )
        s.running = None
        s.calls.clear()
        s.control = None
        s.completion = None
        s.admission = None
        s.compaction = None
        s.last_compaction = None
        s.consolidating.clear()
        s.delegations.clear()
        s.error = ""
        s.quiet_until = 0
        self._flush()
        self._flush_pending()
        self._end_turns("cancelled", reason)
        if s.sandbox_live:
            self._schedule_sandbox_release()
        rotor.record("thread_stopped", {"reason": reason})
        self._report()

    @rotor.on
    async def proposal_accepted(self, msg: messages.ProposalAccepted) -> None:
        _mark_merged(self.state.proposals, (msg.branch, msg.proposal_sha), msg.merge_sha)
        self._report()

    @rotor.on
    async def set_archived(self, msg: messages.SetArchived) -> None:
        s = self.state
        if msg.archived:
            if self._status() != "idle":
                return
            s.archived = True
            self._cancel_all_signals()
        elif s.archived:
            s.archived = False
            if s.pending:
                self._activate()
                return
        self._report()

    @rotor.on
    async def handling_failed(self, msg: rotor.HandlingFailed) -> None:
        # The failed activation rolled back; keep the thread and let an operator resume.
        self.state.error = msg.error.detail
        log.error(
            "thread %s parked after %s: %s",
            self.ref.id,
            msg.error.type,
            msg.error.detail[:1000],
        )
        rotor.record("thread_failed", {"error": msg.error.detail, "branch": self.state.branch})
        self._fail_turns(msg.error.detail)
        self._report()

    # Dispatch

    def _status(self) -> Status:
        s = self.state
        if s.error:
            return "parked"
        if s.archived:
            return "archived"
        if s.consolidating:
            return "handing_off"
        if (
            s.running
            or s.calls
            or s.results
            or s.control
            or s.pending
            or s.admission is not None
            or s.compaction
            or s.delegations
        ):
            return "active"
        return "idle"

    @rotor.on
    async def step(self, msg: messages.Step) -> None:
        s = self.state
        await self._stream_results()
        if (
            s.running
            or environment.Environment.current().now() < s.quiet_until
            or self._status() != "active"
        ):
            return
        if s.compaction is not None:
            await self._request_admission()
            return
        if s.calls:
            if s.calls[0]["tool_name"] == tools.review_proposal.name and s.dirty:
                await self._checkpoint(release=False)
            self._spawn_tool()
            return
        if s.delegations:
            await self._dispatch_delegations()
        if s.control is not None:
            control, s.control = s.control, None
            await self._control(control)
            return
        self._flush()
        self._flush_pending()
        await self._request_admission()

    async def _request_admission(self) -> None:
        """Ask the supervisor for one model call; a park keeps the owed call for a resume."""
        s = self.state
        if s.admission == s.epoch:
            return  # already asked; the supervisor will answer
        s.epoch += 1
        s.admission = s.epoch
        if s.turns >= s.ceiling:
            await self._park(TURN_CEILING)
            return
        self._ensure_turn()
        self.send(self._agent, messages.Admit(self.ref.id, s.epoch))

    def _admitting(self, epoch: int) -> bool:
        return epoch == self.state.admission == self.state.epoch

    @rotor.on
    async def admitted(self, msg: messages.Admitted) -> None:
        s = self.state
        if not self._admitting(msg.epoch) or self._status() != "active":
            return
        s.held_epoch = None
        self._ensure_turn()
        try:
            if s.compaction is not None:
                await self._run_compaction()
                s.epoch += 1  # the main call is still owed; Step asks again for it
            else:
                await self._generate()
                s.admission = None
        except tools.TRANSIENT as error:
            if msg.attempt >= MODEL_RETRIES:
                raise
            rotor.stream.discard()
            rotor.record("model_retry", {"attempt": msg.attempt + 1, "error": str(error)[:200]})
            self.schedule(
                messages.Admitted(msg.epoch, msg.attempt + 1), delay="5s", key="model-retry"
            )
            return
        self.send(self.ref, messages.Step())

    @rotor.on
    async def held(self, msg: messages.Held) -> None:
        """Budget is exhausted: keep the owed call, but do not pin a sandbox while waiting."""
        if not self._admitting(msg.epoch):
            return
        self.state.held_epoch = msg.epoch
        self._fail_turns(BUDGET_HELD)
        if self.state.sandbox_live:
            await self._checkpoint(release=True)
        self._report()

    def _schedule_sandbox_release(self) -> None:
        self.schedule(
            messages.ReleaseSandbox(self.state.epoch),
            delay=environment.Environment.current().config.thread.sandbox_idle_seconds,
            key=SANDBOX_RELEASE_KEY,
        )

    @rotor.on
    async def release_sandbox(self, msg: messages.ReleaseSandbox) -> None:
        """Stop a checkpointed sandbox only if its idle generation is still current.

        fx subagents and open terminals in the sandbox defer the stop to a later timer.
        """
        s = self.state
        held = s.held_epoch is not None and s.held_epoch == s.admission == s.epoch
        if (
            msg.epoch != s.epoch
            or not s.sandbox_live
            or (self._status() not in ("idle", "archived", "handing_off", "parked") and not held)
        ):
            return
        if await tools.release(s.owner, s.branch, s.sandbox, self._chat(), upstream=s.upstream):
            s.sandbox_live = False
            rotor.record("sandbox_released", {"sandbox": s.sandbox})
        else:
            self._schedule_sandbox_release()

    def _tool_key(self, call: dict[str, Any]) -> str:
        return f"tool:{self.state.turns}:{call['tool_call_id']}"

    def _spawn_tool(self) -> None:
        s = self.state
        call = s.calls.pop(0)
        input: dict[str, Any] = {
            "owner": s.owner,
            "branch": s.branch,
            "sandbox": s.sandbox,
            "chat": self._chat(),
            "upstream": s.upstream,
            "call": call,
        }
        if call["tool_name"] == tools.skill_view.name:
            operation: Any = tools.run_skill_view
        elif call["tool_name"] in tools.HATCHERY:
            operation = tools.run_hatchery_tool
            input = {
                "chat_id": s.chat_id,
                "turn_id": (s.turn or {}).get("turn_id", ""),
                "actor_user_id": s.actor_user_id or None,
                "call": call,
            }
        elif call["tool_name"] == tools.review_proposal.name:
            operation = tools.run_review
            review = call["review"]
            task = s.tasks.get(review["task_id"])
            pinned = review["proposal"]
            if (
                task is None
                or task.status == "cancelled"
                or not any(_proposal_key(p) == _proposal_key(pinned) for p in task.proposals)
            ):
                self._result(
                    ai.messages.ToolCallPart.model_validate(call),
                    "the reviewed proposal changed or the task was cancelled",
                    error=True,
                )
                self.send(self.ref, messages.Step())
                return
            input |= {
                "proposal": pinned["branch"],
                "proposal_sha": pinned["sha"],
                "child_process_id": pinned["thread_id"],
                "head": s.checkpoint_sha,
                "task_id": review["task_id"],
            }
        else:
            operation = tools.run_bash
            s.dirty = True
        s.running = call
        self.spawn(operation, input=input, key=self._tool_key(call))

    # One model turn

    async def _generate(self) -> None:
        context_ = await self._prepare()
        reply = await self._infer(context_)
        if reply is None:
            return
        self.state.core_context_revisions = dict(context_.core_revisions)
        self.state.core_updated.clear()
        self.state.updated.clear()
        self._classify(reply)

    async def _prepare(self) -> context.Context:
        """Acquire the sandbox, merge current upstream, and read context."""
        from hatchery.store import agents

        s, env = self.state, environment.Environment.current()
        if not s.sandbox_live:
            agent = await agents.get(s.owner)
            s.repos = list(agent.repos) if agent is not None else []
        sandbox = await tools.acquire(
            s.owner,
            s.branch,
            s.sandbox,
            self._chat(),
            upstream=s.upstream,
            upstream_sha=s.upstream_sha,
        )
        if not s.sandbox_live:
            await tools._sandbox_changed(s.chat_id)
        if s.sandbox_needs_sync:
            sandbox = await self._sync_sandbox(sandbox)
        if not s.base_sha:
            s.base_sha = await env.workspaces.thread_base(s.owner, s.branch, upstream=s.upstream)
        s.sandbox_live = True
        if s.dirty:
            s.context_cache_sha = ""
        await self._refresh(acquired=sandbox)
        cached = None
        if s.context_cache_sha:
            cached = _CONTEXT_CACHE.get((s.owner, s.context_cache_sha))
        if cached is None:
            tree = await sandbox.download(list(context.CONTEXT_PATHS))
            cached = context.Context.from_tree(s.owner, context.layered(tree))
        key = (s.owner, s.checkpoint_sha)
        _CONTEXT_CACHE[key] = cached
        _CONTEXT_CACHE.move_to_end(key)
        while len(_CONTEXT_CACHE) > CONTEXT_CACHE_LIMIT:
            _CONTEXT_CACHE.popitem(last=False)
        s.context_cache_sha = s.checkpoint_sha
        s.core_updated = (
            sorted(
                path
                for path, revision in cached.core_revisions
                if s.core_context_revisions.get(path) != revision
            )
            if s.core_context_revisions
            else []
        )
        return cached

    def _offered_tools(self) -> list[ai.Tool]:
        s = self.state
        return [
            tool.tool
            for tool in tools.TOOLS
            if (tool.name not in tools.CHILD_ONLY or s.parent_thread_id)
            and not (tool.name == tools.start_thread.name and s.linked)
        ]

    async def _infer(self, context_: context.Context) -> ai.messages.Message | None:
        """One main model request, or None when a compaction was queued or the thread parked."""
        from hatchery.agent import telemetry

        s, env = self.state, environment.Environment.current()
        limits = env.config.thread
        history = list(s.messages)
        system = await self._system_prompt(context_)
        tools_for_model = self._offered_tools()
        assert env.model_limits is not None
        input_limit = env.model_limits.input_limit(env.config.model.max_output_tokens)
        model_history = [_model_message(message) for message in history]
        estimate = model_budget.request_tokens(
            system,
            [message.model_dump(mode="json") for message in model_history],
            tools_for_model,
        )
        trigger = min(limits.compact_above_tokens, input_limit)
        previous = s.last_compaction  # a compaction already ran for this turn attempt
        keep_half = max(1, min(limits.keep_recent_messages, (len(history) - 1) // 2))
        if previous is None and compaction.due(
            estimate, len(history), above=trigger, keep=limits.keep_recent_messages
        ):
            s.compaction = Compaction("threshold", limits.keep_recent_messages, estimate)
            return None

        if estimate > input_limit:
            if previous is not None and estimate >= previous.estimate:
                await self._park(
                    "compaction did not reduce the model context below its "
                    f"{input_limit}-token limit"
                )
                return None
            if len(history) <= 2:
                await self._park(
                    f"model context cannot fit: estimated {estimate} input tokens exceeds "
                    f"the {input_limit}-token limit"
                )
                return None
            keep = max(1, previous.keep // 2) if previous is not None else keep_half
            s.compaction = Compaction("fit", keep, estimate)
            return None

        turn_id = (s.turn or {}).get("turn_id", "")
        params = ai.InferenceRequestParams(
            output=ai.OutputParams(max_tokens=env.config.model.max_output_tokens)
        )
        try:
            async with telemetry.use_chat(s.chat_id):
                async with ai.experimental_telemetry.span("hatchery.thread.turn") as span:
                    span.set_attrs(
                        {"chat.id": s.chat_id, "turn.id": turn_id},
                        origin=str((s.turn or {}).get("origin", "")),
                        actor_user_id=s.actor_user_id,
                        thread_id=self.ref.id,
                    )
                    async with ai.stream(
                        env.model,
                        [ai.system_message(system), *model_history],
                        tools=tools_for_model,
                        params=params,
                    ) as response:
                        async for event in response:
                            await rotor.stream(
                                {
                                    "kind": "agent",
                                    "turn_id": turn_id,
                                    "event": event.model_dump(mode="json"),
                                }
                            )
                    reply = response.message
                    if reply is None:
                        raise RuntimeError("model step returned no message")
                    await rotor.stream(
                        {
                            "kind": "agent",
                            "turn_id": turn_id,
                            "event": ai.events.StreamEnd(message=reply).model_dump(mode="json"),
                        }
                    )
        except ai.errors.ProviderAPIError as error:
            failure = provider_errors.classify_request_failure(error)
            if failure is provider_errors.RequestFailure.OTHER:
                raise
            rotor.stream.discard()
            if (previous is not None and previous.provider_rejected) or len(history) <= 2:
                await self._park(f"provider rejected the reduced model request: {error}")
                return None
            s.compaction = Compaction(failure.value, keep_half, estimate)
            return None
        finally:
            telemetry.flush()
        self._account(reply.usage or response.usage)
        self._append_message(reply, {"turn": s.turns + 1})
        if reply.text.strip():
            s.result = reply.text.strip()
            s.replies.append(reply.text)
        s.turns += 1
        s.last_compaction = None
        rotor.record("model_turn", {"turn": s.turns, "text": reply.text, "turn_id": turn_id})
        return reply

    async def _run_compaction(self) -> None:
        """Summarize older history in place; its usage is accounted like any model call."""
        s, env = self.state, environment.Environment.current()
        work = s.compaction
        assert work is not None
        history = list(s.messages)
        assert env.model_limits is not None
        summary_output = min(4096, env.config.model.max_output_tokens)
        summary_input = env.model_limits.input_limit(summary_output)
        usage: ai.types.usage.Usage | None = None
        try:
            compacted, usage = await compaction.compact(
                env.model,
                history,
                keep=work.keep,
                max_input_tokens=summary_input,
                max_output_tokens=summary_output,
            )
        except compaction.CompactionTooLarge:
            compacted = history
        if compacted != history:
            s.messages[:] = compacted
            s.compactions += 1
            rotor.record("compaction", {"kept": len(compacted), "reason": work.reason})
        if usage is not None:
            self._account(usage, main=False)
        s.last_compaction, s.compaction = work, None

    def _classify(self, reply: ai.messages.Message) -> None:
        """Apply inline operations, queue tool calls, and retain one final control."""
        s = self.state
        limits = environment.Environment.current().config.thread
        calls = reply.tool_calls
        if not calls:
            s.control = Idle(note=reply.text)
            return
        inline = {
            tools.signal.name: self._schedule_signal,
            tools.cancel.name: self._cancel_signal,
            tools.secret_request.name: self._request_secret,
            tools.delegate.name: self._delegate,
            tools.message_parent.name: self._message_parent,
            tools.task_roster.name: self._task_roster,
            tools.message_task.name: self._message_task,
            tools.cancel_task.name: self._cancel_task,
            tools.archive_task.name: self._archive_task,
            tools.review_proposal.name: self._review_proposal,
        }
        for index, call in enumerate(calls):
            if call.tool_name in inline:
                inline[call.tool_name](call)
            elif call.tool_name in tools.CONTROL:
                if index != len(calls) - 1:
                    self._result(call, f"{call.tool_name} must be the final tool call", error=True)
                    continue
                try:
                    args = tools.arguments(call)
                    if call.tool_name == tools.complete.name:
                        if not s.parent_thread_id:
                            raise ValueError(
                                "complete is only available to delegated threads; reply normally"
                            )
                        if s.task_cancelled:
                            raise ValueError("this task was cancelled")
                        summary, result = str(args["summary"]).strip(), str(args["result"]).strip()
                        if not summary:
                            raise ValueError("summary must not be empty")
                        if not result:
                            raise ValueError("result must not be empty")
                except ValueError as error:
                    self._result(call, str(error), error=True)
                    continue
                dumped = call.model_dump(mode="json")
                if call.tool_name == tools.complete.name:
                    s.control = Completion(
                        summary,
                        result,
                        [str(item) for item in args["deliverables"] or []],
                        dumped,
                    )
                else:
                    s.control = Idle(note=args.get("note") or "", call=dumped)
            elif call.tool_name not in {tool.name for tool in tools.TOOLS} or (
                call.tool_name == tools.start_thread.name and s.linked
            ):
                self._result(call, f"unknown tool: {call.tool_name}", error=True)
            elif len(s.calls) >= limits.bash_calls_per_turn:
                self._result(call, "too many sandbox tool calls in one turn", error=True)
            else:
                s.calls.append(call.model_dump(mode="json"))

    def _account(self, usage: ai.types.usage.Usage | None, *, main: bool = True) -> None:
        usage = usage or ai.types.usage.Usage()
        self.state.input_tokens += usage.input_tokens
        self.state.output_tokens += usage.output_tokens
        if main:
            self.state.last_input_tokens = usage.input_tokens
        self.send(self._agent, messages.Spent(usage.total_tokens))

    def _result(self, call: ai.messages.ToolCallPart, value: Any, *, error: bool = False) -> None:
        self._add_result(tools.result(call, value, error=error).model_dump(mode="json"))

    def _add_result(self, dumped: dict[str, Any]) -> None:
        self.state.results.append(dumped)
        self.state.unstreamed.append(dumped)

    async def _stream_results(self) -> None:
        """Show settled tool results in the live turn stream."""
        s = self.state
        pending, s.unstreamed = s.unstreamed, []
        if s.turn is None:
            return
        for dumped in pending:
            part = ai.messages.ToolResultPart.model_validate(dumped)
            await rotor.stream(
                {
                    "kind": "agent",
                    "turn_id": s.turn["turn_id"],
                    "event": ai.tool_result(part).model_dump(mode="json"),
                }
            )

    # Inline tools

    def _request_secret(self, call: ai.messages.ToolCallPart) -> None:
        """Record only the name and the operator note; the value never reaches the model."""
        try:
            args = tools.arguments(call)
            name = vault.validate_secret_name(str(args["name"]))
            note = str(args["note"]).strip()
            if not note or len(note) > 1000:
                raise ValueError("secret request note must be 1-1000 characters")
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        self.send(self._agent, messages.SecretRequested(name, note))
        self._result(call, {"name": name, "status": "requested"})

    def _schedule_signal(self, call: ai.messages.ToolCallPart) -> None:
        try:
            args = tools.arguments(call)
            delay = tools.signal_delay(args)
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        signal_id = call.tool_call_id
        scheduled = {
            "id": signal_id,
            "note": args["note"],
            "at": environment.Environment.current().now() + delay,
        }
        self.schedule(
            messages.Signal(signal_id, args["note"]), delay=delay, key=f"signal:{signal_id}"
        )
        self.state.signals[signal_id] = scheduled
        self._result(call, {"status": "scheduled", **scheduled})
        rotor.record("signal_scheduled", scheduled)
        self._report()

    def _cancel_signal(self, call: ai.messages.ToolCallPart) -> None:
        try:
            signal_id = typing.cast(str, tools.arguments(call)["signal_id"])
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        if self.state.signals.pop(signal_id, None) is None:
            self._result(call, f"no pending signal with id {signal_id}", error=True)
            return
        self.unschedule(f"signal:{signal_id}")
        self._result(call, {"status": "cancelled", "signal_id": signal_id})
        rotor.record("signal_cancelled", {"id": signal_id})
        self._report()

    def _cancel_all_signals(self) -> None:
        for signal_id in self.state.signals:
            self.unschedule(f"signal:{signal_id}")
        self.state.signals.clear()

    def _delegate(self, call: ai.messages.ToolCallPart) -> None:
        """Record the task now; DelegateTask goes out once this turn's work is checkpointed."""
        s = self.state
        if s.task_cancelled:
            self._result(call, "cancelled tasks cannot delegate", error=True)
            return
        try:
            args = tools.arguments(call)
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        s.task_sequence += 1
        task_id = f"{s.task_id}:{call.tool_call_id}"
        task = ChildTask(
            task_id=task_id, handle=f"task-{s.task_sequence}", objective=str(args["objective"])
        )
        s.tasks[task_id] = task
        s.delegations.append(
            {
                "task_id": task_id,
                "repository": str(args["repository"]),
                "branch": str(args["branch"]),
            }
        )
        self._result(call, {"status": "queued", "task_id": task.handle})

    async def _dispatch_delegations(self) -> None:
        """Checkpoint so children fork from everything this turn wrote, then delegate."""
        s = self.state
        await self._checkpoint(release=False)
        for delegation in s.delegations:
            task = s.tasks[delegation["task_id"]]
            self.send(
                self._agent,
                messages.DelegateTask(
                    task.task_id,
                    self.ref.id,
                    task.objective,
                    delegation["repository"],
                    delegation["branch"],
                    s.checkpoint_sha,
                    task.handle,
                ),
            )
        s.delegations.clear()

    def _task_ref(self, task_id: str) -> str:
        task = self.state.tasks.get(task_id)
        return task.handle if task is not None else task_id

    def _valid_handles(self) -> str:
        return (
            ", ".join(
                task.handle for task in self.state.tasks.values() if task.status != "rejected"
            )
            or "none"
        )

    def _resolve_task(self, reference: str) -> ChildTask | None:
        """Resolve a handle or internal id, or one unique id suffix the model mangled."""
        tasks = {
            task_id: task for task_id, task in self.state.tasks.items() if task.status != "rejected"
        }
        direct = [t for t in tasks.values() if reference in (t.task_id, t.handle)]
        if len(direct) == 1:
            return direct[0]
        suffix = reference.rsplit(":", 1)[-1]
        recovered = [t for t in tasks.values() if t.task_id.endswith(f":{suffix}")]
        return recovered[0] if len(recovered) == 1 else None

    def _task_target(
        self,
        call: ai.messages.ToolCallPart,
        *,
        require: str | None = None,
        allow_cancelled: bool = False,
    ) -> tuple[dict[str, Any], ChildTask] | None:
        """Validate a task tool call and resolve its target; answers the call on failure."""
        try:
            args = tools.arguments(call)
            if require is not None and not str(args[require]).strip():
                raise ValueError(f"{require} must not be empty")
        except ValueError as error:
            self._result(call, str(error), error=True)
            return None
        reference = str(args["task_id"])
        task = self._resolve_task(reference)
        if task is None:
            self._result(
                call,
                f"unknown direct task `{reference}`; valid task IDs: {self._valid_handles()}",
                error=True,
            )
            return None
        if task.status == "cancelled" and not allow_cancelled:
            self._result(call, f"task `{task.handle}` is cancelled", error=True)
            return None
        return args, task

    def _message_task(self, call: ai.messages.ToolCallPart) -> None:
        target = self._task_target(call, require="text")
        if target is None:
            return
        args, task = target
        self.send(
            self._agent, messages.MessageTask(task.task_id, self.ref.id, str(args["text"]).strip())
        )
        self._result(call, {"status": "accepted", "task_id": task.handle})

    def _message_parent(self, call: ai.messages.ToolCallPart) -> None:
        s = self.state
        if not s.parent_thread_id:
            self._result(
                call,
                "message_parent is only available to delegated threads; reply normally",
                error=True,
            )
            return
        if s.task_cancelled:
            self._result(call, "this task was cancelled", error=True)
            return
        try:
            text = str(tools.arguments(call)["text"]).strip()
            if not text:
                raise ValueError("text must not be empty")
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        self.send(self._agent, messages.MessageParent(s.task_id, self.ref.id, text))
        self._result(call, {"status": "sent", "task_id": s.task_handle or s.task_id})

    def _cancel_task(self, call: ai.messages.ToolCallPart) -> None:
        target = self._task_target(call, require="reason")
        if target is None:
            return
        args, task = target
        task.status = "cancelled"
        self.send(
            self._agent,
            messages.CancelTask(task.task_id, self.ref.id, str(args["reason"]).strip()),
        )
        self._result(call, {"status": "accepted", "task_id": task.handle})

    def _archive_task(self, call: ai.messages.ToolCallPart) -> None:
        target = self._task_target(call, allow_cancelled=True)
        if target is None:
            return
        _, task = target
        self.send(self._agent, messages.ArchiveTask(task.task_id, self.ref.id))
        self._result(call, {"status": "accepted", "task_id": task.handle})

    def _task_roster(self, call: ai.messages.ToolCallPart) -> None:
        try:
            tools.arguments(call)
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        roster = [
            {
                **dataclasses.asdict(task),
                "task_id": task.handle,
                "status": task.status if task.status in TASK_STATUSES else "working",
            }
            for task in self.state.tasks.values()
            if task.status != "rejected"
        ]
        self._result(call, {"tasks": roster})

    def _review_proposal(self, call: ai.messages.ToolCallPart) -> None:
        """Rejections go to the child through the supervisor; approvals merge in the tool queue."""
        try:
            args = tools.arguments(call)
            decision = str(args["decision"])
            section = str(args["section"])
            if decision not in ("approve", "reject"):
                raise ValueError("decision must be approve or reject")
            if section not in workspace_repo.SECTIONS:
                raise ValueError("section must be workspace or wiki")
            if decision == "reject" and not str(args["comment"]).strip():
                raise ValueError("a rejection needs a comment")
        except ValueError as error:
            self._result(call, str(error), error=True)
            return
        target = self._task_target(call)
        if target is None:
            return
        _, task = target
        if decision == "reject":
            self.send(
                self._agent,
                messages.ReviewTaskProposal(
                    task.task_id, self.ref.id, section, decision, str(args["comment"])
                ),
            )
            self._result(
                call, {"status": "rejected", "task_id": args["task_id"], "section": section}
            )
            return
        proposal = next(
            (p for p in task.proposals if p.get("section") == section and not p.get("merged")),
            None,
        )
        if proposal is None:
            self._result(call, "no open proposal for that task and section", error=True)
            return
        # Pin the reviewed proposal: a re-proposal before the merge runs must be re-reviewed.
        queued = call.model_dump(mode="json")
        queued["review"] = {"task_id": task.task_id, "proposal": dict(proposal)}
        self.state.calls.append(queued)

    # Transcript

    async def _system_prompt(self, context_: context.Context) -> str:
        from hatchery.store import agents

        s = self.state
        system = context_.system_prompt()
        sections = []
        agent = await agents.get(s.owner)
        if agent is not None:
            sections.append(context.hatchery_prompt(agent, linked=s.linked))
        if s.parent_thread_id:
            sections.append(
                "# Delegated task\n"
                f"Task handle: `{s.task_handle or s.task_id}`\n"
                f"Objective: {s.objective}\n\n"
                "Stay focused on this objective. Put disposable task-specific notes under "
                "`/workspace/scratchpad/`; this sandbox-local directory may survive across "
                "turns but is not checkpointed and can be lost. Edit shared memory only "
                "when the objective requires durable changes. Your memory changes become "
                "proposals your parent must approve. Normal reply text is for the operator "
                "viewing this thread. "
                "Use `message_parent` for every question, blocker, progress update, or "
                "partial result your parent should receive; each message is delivered "
                "immediately and is never replaced. When the assignment is done, call "
                "`complete` as the final tool with a concise summary, the complete result, "
                "and artifact paths or URLs. Do not call `idle` after `complete`; completion "
                "performs the final handoff. A later operator or parent message reopens the "
                "assignment. Messages headed `Message from parent thread:` are from the "
                "thread that delegated it; unmarked user messages are from the operator."
            )
        if s.updated or s.conflicts or s.core_updated:
            lines = []
            if s.core_updated:
                lines.append(
                    "Always-loaded context changed since your last model turn: "
                    + ", ".join(s.core_updated)
                    + ". The current snapshots below supersede stale saved-memory claims."
                )
            if s.updated:
                lines.append(
                    "Updated from shared memory since your last turn: " + ", ".join(s.updated)
                )
            if s.conflicts:
                lines.append("Conflict markers to resolve before idling: " + ", ".join(s.conflicts))
            sections.append("# Shared memory\n" + "\n".join(lines))
        if s.signals:
            scheduled = [
                f"- `{item['id']}` \N{EM DASH} Unix timestamp {item['at']}: {item['note']}"
                for item in s.signals.values()
            ]
            sections.append(
                "# Scheduled signals\n"
                "These signals remain active while you converse. Use `cancel` with an ID to stop "
                "one.\n" + "\n".join(scheduled)
            )
        return system + ("\n\n" + "\n\n".join(sections) if sections else "")

    def _append_message(
        self, message: ai.messages.Message, metadata: dict[str, Any] | None = None
    ) -> None:
        """Keep the time and provenance with the durable transcript, outside model input."""
        dumped = {
            **message.model_dump(mode="json"),
            "timestamp": environment.Environment.current().now(),
            **(metadata or {}),
        }
        self.state.messages.append(dumped)
        self.state.unprojected.append(dumped)

    def _flush(self) -> None:
        """Commit the turn's tool results as one tool message."""
        s = self.state
        if s.results:
            parts = [ai.messages.ToolResultPart.model_validate(r) for r in s.results]
            self._append_message(ai.tool_message(*parts))
            s.results.clear()

    def _flush_pending(self) -> None:
        """Record accepted inputs before admission makes model output possible.

        Chat messages keep their IDs. Inputs the thread made itself (signals, task
        coordination, parent prompts, repairs) are marked so the chat shows them but
        the ingress cursor never feeds them back.
        """
        s = self.state
        flushed = {prompt.get("request_id") for prompt in s.pending}
        started = [turn for turn in s.turn_queue if turn["turn_id"] in flushed]
        if started:
            # A turn whose input enters history answers from here; earlier ones end.
            s.turn_queue = [turn for turn in s.turn_queue if turn["turn_id"] not in flushed]
            if s.turn is not None:
                self._finish_turn("completed")
            for turn in started[:-1]:
                self._spawn_finish(turn, "completed")
            self._begin_turn(started[-1])
        for prompt in s.pending:
            metadata = {
                key: value for key, value in prompt.items() if key not in ("text", "message")
            }
            if not metadata["request_id"]:
                del metadata["request_id"]
            if prompt.get("message") is not None:
                message = ai.messages.Message.model_validate(prompt["message"])
            else:
                message = ai.user_message(prompt["text"])
                message.provider_metadata = {
                    "hatchery": {"origin": "thread", "source": prompt["source"]}
                }
            self._append_message(message, metadata)
        s.pending.clear()

    # Hatchery turns

    def _begin_turn(self, turn: dict[str, Any]) -> None:
        s = self.state
        s.turn = turn
        s.replies.clear()
        self.spawn(
            announce_thread_turn,
            input={"chat_id": s.chat_id},
            key=f"announce:{turn['turn_id']}",
            detached=True,
        )
        rotor.record("turn.started", {"turn_id": turn["turn_id"], "origin": turn["origin"]})

    def _ensure_turn(self) -> None:
        """Model output always streams under a turn; self-started work gets its own."""
        s = self.state
        if s.turn is not None:
            return
        s.turn_sequence += 1
        digest = hashlib.sha256(self.ref.id.encode()).hexdigest()[:16]
        turn = {
            "turn_id": f"turn_thread_{digest}_{s.turn_sequence}",
            "origin": "thread",
            "task_id": None,
            "actor_user_id": None,
        }
        self.spawn(
            register_thread_turn,
            input={"chat_id": s.chat_id, "turn_id": turn["turn_id"], "process_id": self.ref.id},
            key=f"register:{turn['turn_id']}",
        )
        self._begin_turn(turn)

    def _spawn_finish(
        self,
        turn: dict[str, Any],
        state: Literal["completed", "failed", "cancelled"],
        error: str | None = None,
        replies: list[str] | None = None,
        projected: list[dict[str, Any]] | None = None,
    ) -> None:
        s = self.state
        turn_id = turn["turn_id"]
        key = f"finish:{turn_id}" if state == "completed" else f"finish:{turn_id}:{state}"
        if key in s.finishing:
            return
        s.finishing[key] = {"turn": turn, "state": state, "error": error}
        self.spawn(
            finish_turn,
            input={
                "chat_id": s.chat_id,
                "turn_id": turn_id,
                "process_id": self.ref.id,
                "state": state,
                "task_id": turn.get("task_id"),
                "error": error,
                "replies": list(replies or []),
                "messages": list(projected or []),
            },
            key=key,
        )

    def _finish_turn(
        self, state: Literal["completed", "failed", "cancelled"], error: str | None = None
    ) -> None:
        """End the streaming turn: its new messages, replies, and terminal projection."""
        s = self.state
        if s.turn is None:
            if s.unprojected:
                self._ensure_turn()
            else:
                return
        assert s.turn is not None
        turn, s.turn = s.turn, None
        self._spawn_finish(turn, state, error, s.replies, s.unprojected)
        s.replies.clear()
        s.unprojected.clear()

    def _end_turns(self, state: Literal["failed", "cancelled"], error: str) -> None:
        """End the streaming turn and every queued turn; queued input stays pending."""
        s = self.state
        if s.turn is not None or s.unprojected:
            self._finish_turn(state, error)
        for turn in s.turn_queue:
            self._spawn_finish(turn, state, error)
        s.turn_queue.clear()

    def _fail_turns(self, error: str) -> None:
        self._end_turns("failed", error)

    @rotor.on(finish_turn.Done)
    async def turn_finished(self, msg: rotor.ChildDone) -> None:
        finishing = self.state.finishing.pop(msg.key, None)
        if finishing is None:
            return
        turn_id, state = finishing["turn"]["turn_id"], finishing["state"]
        data = {"turn_id": turn_id, "chat_id": self.state.chat_id}
        if finishing["error"] is not None:
            data["error"] = finishing["error"]
        try:
            await rotor.stream({"kind": "lifecycle", "type": f"turn.{state}", **data})
        except Exception:
            log.exception("failed to stream the end of turn %s", turn_id)
        rotor.record(f"turn.{state}", data)

    @rotor.on(finish_turn.Failed)
    async def turn_finish_failed(self, msg: rotor.ChildFailed) -> None:
        finishing = self.state.finishing.pop(msg.key, None)
        if finishing is None:
            return
        log.error("turn %s could not finish: %s", finishing["turn"]["turn_id"], msg.reason)
        if finishing["state"] == "completed":
            self._spawn_finish(finishing["turn"], "failed", str(msg.reason))
        else:
            data = {
                "turn_id": finishing["turn"]["turn_id"],
                "chat_id": self.state.chat_id,
                "error": str(msg.reason),
            }
            rotor.record("turn.failed", data)

    # Children: sandbox tools and consolidation

    @rotor.on(rotor.ChildDone)
    async def child_done(self, msg: rotor.ChildDone) -> None:
        if msg.process == tools.consolidate.NAME:
            await self._consolidated(msg)
            return
        call = self._settle(msg.key)
        if call is None:
            return
        if call["tool_name"] == tools.review_proposal.name:
            self._reviewed(msg.output)
        else:
            self._add_result(msg.output)
            if call["tool_name"] == tools.start_thread.name:
                output = ai.messages.ToolResultPart.model_validate(msg.output).result
                if isinstance(output, dict) and output.get("status") == "sent":
                    self.state.linked = True
        await self._after_tool()

    @rotor.on(rotor.ChildFailed)
    async def child_failed(self, msg: rotor.ChildFailed) -> None:
        if msg.process == tools.consolidate.NAME:
            # Retries are exhausted; park with the section still queued so a resume redoes it.
            if self._current_consolidation(msg.key):
                rotor.record(
                    "consolidation_failed",
                    {"section": self.state.consolidating[0], "reason": str(msg.reason)},
                )
                await self._park(str(msg.reason))
            return
        call = self._settle(msg.key)
        if call is None:
            return
        part = ai.messages.ToolCallPart.model_validate(call)
        self._result(part, str(msg.reason), error=True)
        if call["tool_name"] == tools.bash.name:
            # Only failures that could not prove the sandbox stopped reach this handler.
            # Wait out the command deadline before touching the filesystem again.
            env = environment.Environment.current()
            try:
                timeout = tools.bash_timeout(part, env.config.thread.command_timeout_seconds)
            except ValueError:
                timeout = env.config.thread.command_timeout_seconds
            self.state.quiet_until = env.now() + timeout
            self.schedule(messages.Step(), delay=timeout, key="quiescence")
            return
        await self._after_tool()

    def _settle(self, key: str) -> dict[str, Any] | None:
        """Match a child verdict to the live tool call and clear it."""
        running = self.state.running
        if running is None or key != self._tool_key(running):
            return None
        self.state.running = None
        return running

    async def _after_tool(self) -> None:
        """Continue the turn, unless a stop was deferred until this tool settled."""
        s = self.state
        reason, s.interrupt_after_tool = s.interrupt_after_tool, ""
        if reason:
            await self._stop(reason)
        else:
            self.send(self.ref, messages.Step())

    def _reviewed(self, output: dict[str, Any]) -> None:
        """An approval merged (or was refused); align the branch, sandbox, and task views."""
        s = self.state
        self._add_result(output["result"])
        if not output.get("merge_sha"):
            return
        s.checkpoint_sha = output["merge_sha"]
        s.sandbox_needs_sync = not bool(output.get("sandbox_synced"))
        key = (output["branch"], output["proposal_sha"])
        for task in s.tasks.values():
            _mark_merged(task.proposals, key, output["merge_sha"])
        if any(_affects_context(path) for path in output.get("changed", [])):
            s.context_cache_sha = ""
        self.send(
            self._agent,
            messages.ProposalMerged(
                str(output["task_id"]),
                self.ref.id,
                str(output["branch"]),
                str(output["proposal_sha"]),
                str(output["merge_sha"]),
            ),
        )

    # Workspace

    async def _control(self, decision: Idle | Completion) -> None:
        s = self.state
        call = ai.messages.ToolCallPart.model_validate(decision.call) if decision.call else None
        if s.pending:  # steering arrived mid-turn: take another turn instead of stopping
            if call is not None:
                self._result(call, "interrupted: a new prompt is pending")
            self.send(self.ref, messages.Step())
            return
        if isinstance(decision, Completion):
            s.completion = decision
            s.summary = decision.summary
            if call is not None:
                self._result(
                    call,
                    {"status": "finalizing", "task_id": s.task_handle or s.task_id},
                )
        else:
            if call is not None:
                self._result(call, f"idle: {decision.note}")
            if s.completion is None:
                s.summary = decision.note
        self._flush()
        await self._stream_results()
        self._finish_turn("completed")
        await self._checkpoint(release=False)
        self._schedule_sandbox_release()
        s.handoffs += 1
        s.consolidating = list(workspace_repo.SECTIONS)
        self._consolidate_next()
        self._report()

    async def _checkpoint(self, *, release: bool, acquired: provider.Sandbox | None = None) -> None:
        s = self.state
        if release:
            self.unschedule(SANDBOX_RELEASE_KEY)
        if not s.sandbox_live:
            return
        if release and await tools.busy(s.chat_id, s.sandbox):
            # fx subagents or terminals still use the sandbox: stop it later instead.
            release = False
            self._schedule_sandbox_release()
        if s.sandbox_needs_sync:
            acquired = await self._sync_sandbox(acquired)
        s.checkpoint_sha = await tools.checkpoint(
            s.owner,
            s.branch,
            s.sandbox,
            self._chat(),
            process_id=self.ref.id,
            operation_id=f"checkpoint:{s.turns}:{s.epoch}:{len(s.messages)}",
            release=release,
            acquired=acquired,
            upstream=s.upstream,
        )
        s.dirty = False
        s.sandbox_live = not release

    async def _sync_sandbox(self, acquired: provider.Sandbox | None = None) -> provider.Sandbox:
        """Install the authoritative thread branch before any stale sandbox can checkpoint."""
        s, env = self.state, environment.Environment.current()
        sandbox = acquired or await tools.acquire(
            s.owner, s.branch, s.sandbox, self._chat(), upstream=s.upstream
        )
        files = await env.workspaces.materialize(s.owner, s.branch, upstream=s.upstream)
        await sandbox.replace(["self", "wiki"], files)
        s.sandbox_live = True
        s.sandbox_needs_sync = False
        s.dirty = False
        s.context_cache_sha = ""
        return sandbox

    async def _refresh(self, *, acquired: provider.Sandbox | None = None) -> tuple[str, ...]:
        """Merge upstream into the thread and install it; returns the paths left with markers."""
        s = self.state
        if s.dirty or not s.checkpoint_sha:
            await self._checkpoint(release=False, acquired=acquired)
        refreshed = await tools.refresh(
            s.owner,
            s.branch,
            s.sandbox,
            self._chat(),
            head=s.checkpoint_sha,
            base=s.base_sha,
            process_id=self.ref.id,
            acquired=acquired,
            upstream=s.upstream,
        )
        s.checkpoint_sha = refreshed.sha
        s.sandbox_live = True
        s.updated = list(refreshed.updated)
        s.conflicts = sorted(set(s.conflicts) | set(refreshed.conflicts))
        if any(_affects_context(path) for path in refreshed.changed):
            s.context_cache_sha = ""
        rotor.record(
            "memory_refreshed",
            {"upstream": refreshed.upstream, "conflicts": list(refreshed.conflicts)},
        )
        return refreshed.conflicts

    async def _park(self, reason: str) -> None:
        await self._checkpoint(release=True)
        self.state.error = reason
        rotor.record("thread_parked", {"reason": reason})
        self._fail_turns(reason)
        self._report()

    # Consolidation: merging the checkpoint into upstream, section by section

    def _consolidation_key(self) -> str:
        s = self.state
        return f"consolidate:{s.handoffs}:{s.consolidating[0]}"

    def _current_consolidation(self, key: str) -> bool:
        return bool(self.state.consolidating) and key == self._consolidation_key()

    def _consolidate_next(self) -> None:
        s, env = self.state, environment.Environment.current()
        section = s.consolidating[0]
        self.spawn(
            tools.consolidate,
            input={
                "owner": s.owner,
                "branch": s.branch,
                "section": section,
                "head": s.checkpoint_sha,
                "summary": s.summary,
                "policy": "parent" if s.parent_thread_id else getattr(env.config.review, section),
                "process_id": self.ref.id,
                "upstream": s.upstream,
            },
            key=self._consolidation_key(),
        )

    async def _consolidated(self, msg: rotor.ChildDone) -> None:
        s = self.state
        if not self._current_consolidation(msg.key):
            return
        output = msg.output
        if output.get("conflicts"):
            s.consolidating.clear()
            await self._repair(output["conflicts"])
            return
        if output.get("withdrawn"):
            s.proposals = [p for p in s.proposals if p["branch"] != output["withdrawn"]]
        if output.get("proposal"):
            s.proposals = [p for p in s.proposals if p["section"] != output["section"]]
            s.proposals.append(output["proposal"])
        prefix = "self/" if output["section"] == "workspace" else "wiki/"
        s.conflicts = [path for path in s.conflicts if not path.startswith(prefix)]
        s.consolidating.pop(0)
        if s.consolidating:
            self._consolidate_next()
        else:
            self._finish_handoff()

    def _finish_handoff(self) -> None:
        """Finalize an immutable completion, then process work queued during publication."""
        s = self.state
        reopens = bool(s.pending) and any(
            prompt.get("source") not in ("maintenance", "signal", "task") for prompt in s.pending
        )
        if s.completion is not None:
            completion, s.completion = s.completion, None
            s.task_status = "completed"
            s.completion_summary = completion.summary
            s.completion_result = completion.result
            s.deliverables = list(completion.deliverables)
            self.send(
                self._agent,
                messages.TaskCompleted(
                    s.task_id,
                    self.ref.id,
                    "working" if reopens else "completed",
                    completion.summary,
                    completion.result,
                    list(completion.deliverables),
                    list(s.proposals),
                ),
            )
        if s.pending:
            if reopens:
                self._reopen_assignment()
            self._activate()
        else:
            self._report()

    async def _repair(self, paths: list[str]) -> None:
        """Upstream and this thread changed the same files; let the model fix them."""
        conflicts = await self._refresh()
        listed = "\n".join(f"- {path}" for path in conflicts or paths)
        upstream = "parent" if self.state.parent_thread_id else "main"
        self._queue_input(
            "Shared memory changed files you also changed. They now contain "
            f"`<<<<<<< {upstream}` / `>>>>>>> thread` conflict markers:\n"
            f"{listed}\n\n"
            "Edit each file to its intended content, preserving both sides' intent, "
            "then request idle again. Handle this maintenance silently.",
            "maintenance",
        )
        self._activate()

    # Reporting

    def _report(self) -> None:
        s = self.state
        if s.parent_thread_id and s.task_status not in TASK_STATUSES:
            s.task_status = "working"
        self.send(
            self._agent,
            messages.ThreadReport(
                thread_id=self.ref.id,
                status=self._status(),
                summary=s.summary,
                result=s.result,
                signals=list(s.signals.values()),
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                turns=s.turns,
                ceiling=s.ceiling,
                compactions=s.compactions,
                checkpoint_sha=s.checkpoint_sha,
                base_sha=s.base_sha,
                handoffs=s.handoffs,
                archived=s.archived,
                task_status=s.task_status,
                completion_summary=s.completion_summary,
                completion_result=s.completion_result,
                deliverables=s.deliverables,
                proposals=s.proposals,
                error=s.error,
            ),
        )

    def _activity(self) -> dict[str, Any]:
        s = self.state
        return {
            "status": self._status(),
            "sandbox_active": s.sandbox_live,
            "pending_prompts": len(s.pending),
            "queued_tools": len(s.calls),
            "running_tool": s.running,
            "consolidating": s.consolidating,
            "awaiting_admission": s.admission == s.epoch,
            "budget_held": s.held_epoch == s.admission == s.epoch,
            "compaction": dataclasses.asdict(s.compaction) if s.compaction is not None else None,
            "quiet_until": s.quiet_until,
            "turn": s.turn,
            "queued_turns": len(s.turn_queue),
        }

    @rotor.query
    def activity(self) -> dict[str, Any]:
        """Small observation projection; never reads the conversation journal."""
        return self._activity()

    @rotor.query
    async def details(self) -> dict[str, Any]:
        s = self.state
        public = (
            "owner",
            "chat_id",
            "branch",
            "sandbox",
            "upstream",
            "parent_thread_id",
            "depth",
            "objective",
            "summary",
            "result",
            "error",
            "input_tokens",
            "output_tokens",
            "turns",
            "ceiling",
            "compactions",
            "checkpoint_sha",
            "base_sha",
            "handoffs",
            "archived",
            "task_id",
            "task_handle",
            "task_status",
            "completion_summary",
            "completion_result",
            "deliverables",
            "proposals",
        )
        return {
            **{name: getattr(s, name) for name in public},
            "task_status": s.task_status if s.task_status in TASK_STATUSES else "working",
            "thread_id": self.ref.id,
            "status": self._status(),
            "signals": list(s.signals.values()),
            "messages": list(s.messages),
            "pending": s.pending,
            "running": s.running,
            "tasks": {task_id: dataclasses.asdict(task) for task_id, task in s.tasks.items()},
            "activity": self._activity(),
            "tool_progress": {
                "running": s.running["tool_call_id"] if s.running else None,
                "queued": [call["tool_call_id"] for call in s.calls],
                "results": s.results,
            },
        }
