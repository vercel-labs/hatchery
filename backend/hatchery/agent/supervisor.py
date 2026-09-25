"""One agent's supervisor: routes turns to threads, supervises them, holds the budget.

Ported from agentmesh `agent/agent.py` (`Agent`). A supervisor is a keyed Rotor
singleton (`key=<agent id>`, `scope="agents"`), so starting it is get-or-create. It
never touches Git, sandboxes, or models itself; threads do the work and report back.

Every thread runs exactly one task: a root thread runs the chat a person opened
(`chat:<chat id>`), a child thread runs the task its parent delegated. The task id is
the thread's spawn key, so one `ThreadRecord` describes both and the delegation tree is
plain supervisor state rather than the Rotor process tree.

Ingress: `start_turn` (UI, Slack/GitHub, fx completions, prompt jobs) sends a
`TurnInput` carrying only the chat messages after the chat's thread cursor, so
history the thread compacted is never fed back. `prompt` lands handler and schedule
`prompt()` effects the same way, in a chat keyed by the effect's conversation key.

Secret requests (`secret_request` tool) are recorded here as name and note only; the
operator sets values through the vault, which settles the request.
"""

import dataclasses
import hashlib
import logging
import typing
import uuid
from typing import Any

import ai
import rotor

from hatchery import config, environment, messages
from hatchery.agent import budget, thread, tools
from hatchery.worker import provider

SCOPE = "agents"
FINISHED = ("completed", "cancelled")
TASK_STATUSES = ("working", *FINISHED)
log = logging.getLogger(__name__)


def _task_status(value: str) -> str:
    return value if value in TASK_STATUSES else "working"


def process_id(agent_id: str) -> str:
    return rotor.singleton_id(Supervisor, agent_id, scope=SCOPE)


def root_key(chat_id: str) -> str:
    return f"chat:{chat_id}"


def _proposal_key(proposal: dict[str, Any]) -> tuple[Any, Any]:
    return proposal.get("branch"), proposal.get("sha")


@rotor.state
class ThreadRecord:
    """One thread and the task it runs."""

    thread_id: str = ""
    chat_id: str = ""
    task_id: str = ""
    task_handle: str = ""  # task-N, unique among one parent's children; "" for roots
    parent_thread_id: str = ""
    children: list[str] = dataclasses.field(default_factory=list)  # in delegation order
    depth: int = 0
    branch: str = ""  # the thread's Git branch
    upstream: str = "main"  # the branch it merges from and proposes to
    sandbox: str = ""
    objective: str = ""
    repository: str = ""  # coding repository and branch named at delegation
    repository_branch: str = ""
    # Execution, from the thread's reports.
    status: str = "active"
    live: bool = True
    archived: bool = False
    error: str = ""
    cleanup: str = ""  # "" | pending | running | complete | failed
    summary: str = ""
    result: str = ""
    signals: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    ceiling: int = 0
    compactions: int = 0
    checkpoint_sha: str = ""
    base_sha: str = ""
    handoffs: int = 0
    proposals: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Assignment state is owned by the child: working | completed | cancelled.
    task_status: str = "working"
    completion_summary: str = ""
    completion_result: str = ""
    deliverables: list[str] = dataclasses.field(default_factory=list)
    cancel_reason: str = ""

    @property
    def busy(self) -> bool:
        return self.live or self.cleanup in ("pending", "running")

    @property
    def settled(self) -> bool:
        """Nothing running and no cleanup outstanding: safe to archive."""
        return self.cleanup not in ("pending", "running") and (
            not self.live or self.status in ("idle", "archived")
        )

    def absorb_proposals(self, proposals: list[dict[str, Any]]) -> None:
        """Take the child's current proposals, keeping merges the parent already recorded."""
        merged = {_proposal_key(p): p for p in self.proposals if p.get("merged")}
        self.proposals = [{**p, **merged.get(_proposal_key(p), {})} for p in proposals]

    def mark_merged(self, branch: str, proposal_sha: str, merge_sha: str) -> None:
        for proposal in self.proposals:
            if _proposal_key(proposal) == (branch, proposal_sha):
                proposal["merged"] = True
                proposal["merge_sha"] = merge_sha


@rotor.state
class SupervisorState:
    agent_id: str = ""
    retiring: bool = False
    budget: budget.Budget = dataclasses.field(default_factory=budget.Budget)
    threads: dict[str, ThreadRecord] = dataclasses.field(default_factory=dict)
    requests: dict[str, str] = dataclasses.field(default_factory=dict)  # turn_id -> thread_id
    waiting: dict[str, int] = dataclasses.field(default_factory=dict)  # thread -> held epoch
    secret_requests: dict[str, str] = dataclasses.field(default_factory=dict)  # name -> note


class Supervisor(rotor.DurableProcess[SupervisorState]):
    @rotor.on
    async def start(self, msg: rotor.Start) -> None:
        env = environment.Environment.current()
        self.state.agent_id = msg.input["agent_id"]
        self.state.budget.ceiling = env.config.budget.tokens_per_day
        self._roll_day()

    # Lookups

    def _task(self, task_id: str) -> ThreadRecord | None:
        return self.state.threads.get(rotor.child_id(self.ref.id, task_id))

    def _live(self, thread_id: str) -> ThreadRecord | None:
        thread_ = self.state.threads.get(thread_id)
        return thread_ if thread_ is not None and thread_.live else None

    def _subtree(self, root: str) -> list[str]:
        """The thread and its descendants in preorder."""
        result: list[str] = []
        stack = [root]
        while stack:
            thread_id = stack.pop()
            if thread_id not in result:
                result.append(thread_id)
                stack.extend(reversed(self.state.threads[thread_id].children))
        return result

    def _children(self, parent_thread_id: str) -> list[ThreadRecord]:
        return [t for t in self.state.threads.values() if t.parent_thread_id == parent_thread_id]

    def _child_roster(self, parent_thread_id: str) -> list[dict[str, object]]:
        return [
            {
                "task_id": child.task_handle,
                "thread_id": child.thread_id,
                "objective": child.objective,
                "status": _task_status(child.task_status),
            }
            for child in self._children(parent_thread_id)
        ]

    # Routing

    def _spawn_thread(
        self,
        record: ThreadRecord,
        *,
        prompts: list[dict[str, Any]] | None = None,
        turns: list[dict[str, Any]] | None = None,
        upstream_sha: str = "",
        actor_user_id: str | None = None,
    ) -> str:
        """Start the thread for one task record; the task id doubles as the child key."""
        s = self.state
        parent = s.threads.get(record.parent_thread_id)
        ref = self.spawn(
            thread.AgentThread,
            input={
                "owner": s.agent_id,
                "chat_id": record.chat_id,
                "parent_chat_id": parent.chat_id if parent is not None else None,
                "actor_user_id": actor_user_id,
                "prompts": list(prompts or []),
                "turns": list(turns or []),
                "task_id": record.task_id,
                "task_handle": record.task_handle,
                "parent_thread_id": record.parent_thread_id,
                "upstream": record.upstream,
                "upstream_sha": upstream_sha,
                "depth": record.depth,
                "objective": record.objective,
            },
            key=record.task_id,
        )
        record.thread_id = ref.id
        record.chat_id = record.chat_id or thread.child_chat_id(ref.id)
        record.branch = environment.Environment.current().workspaces.thread_branch(
            s.agent_id, ref.id
        )
        record.sandbox = provider.sandbox_name(ref.id)
        s.threads[ref.id] = record
        if record.parent_thread_id:
            s.threads[record.parent_thread_id].children.append(ref.id)
        return ref.id

    def _chat_thread(self, chat_id: str) -> ThreadRecord | None:
        return next((t for t in self.state.threads.values() if t.chat_id == chat_id), None)

    @rotor.on
    async def turn_input(self, msg: messages.TurnInput) -> None:
        s = self.state
        if msg.turn_id in s.requests:
            return
        existing = self._chat_thread(msg.chat_id)
        if s.retiring or (existing is not None and not existing.live):
            reason = "the agent is retiring" if s.retiring else "the chat's thread has ended"
            rotor.record("turn_rejected", {"turn_id": msg.turn_id, "reason": reason})
            self.spawn(
                thread.finish_turn,
                input={
                    "chat_id": msg.chat_id,
                    "turn_id": msg.turn_id,
                    "process_id": rotor.child_id(self.ref.id, root_key(msg.chat_id)),
                    "state": "failed",
                    "task_id": msg.task_id,
                    "error": reason,
                },
                key=f"reject:{msg.turn_id}",
            )
            return
        if existing is None:
            root = ThreadRecord(task_id=root_key(msg.chat_id), chat_id=msg.chat_id)
            thread_id = self._spawn_thread(
                root, turns=[dataclasses.asdict(msg)], actor_user_id=msg.actor_user_id
            )
        else:
            self._unarchive(existing)
            self.send(existing.thread_id, msg)
            thread_id = existing.thread_id
        s.requests[msg.turn_id] = thread_id
        rotor.record("turn_routed", {"turn_id": msg.turn_id, "thread_id": thread_id})

    @rotor.on
    async def report(self, msg: messages.ThreadReport) -> None:
        record = self._live(msg.thread_id)
        if record is None:
            return
        record.status = msg.status
        record.summary = msg.summary
        record.result = msg.result
        record.signals = msg.signals
        record.input_tokens = msg.input_tokens
        record.output_tokens = msg.output_tokens
        record.turns = msg.turns
        record.ceiling = msg.ceiling
        record.compactions = msg.compactions
        record.checkpoint_sha = msg.checkpoint_sha
        record.base_sha = msg.base_sha
        record.handoffs = msg.handoffs
        record.archived = msg.archived
        record.error = msg.error
        if record.parent_thread_id and record.task_status != "cancelled":
            record.task_status = _task_status(msg.task_status)
            record.completion_summary = msg.completion_summary
            record.completion_result = msg.completion_result
            record.deliverables = list(msg.deliverables)
        record.absorb_proposals(msg.proposals)
        if msg.status == "idle":
            self.state.waiting.pop(msg.thread_id, None)

    @rotor.on
    async def archive_thread(self, msg: messages.ArchiveThread) -> None:
        """Operator archive of a root cascades through an idle subtree; unarchive does not."""
        record = self._live(msg.thread_id)
        if record is None:
            return
        if not msg.archived:
            self._unarchive(record)
            return
        if record.parent_thread_id:
            rotor.record(
                "archive_skipped",
                {"thread_id": msg.thread_id, "reason": "only root threads can be archived"},
            )
            return
        subtree = self._subtree(msg.thread_id)
        blocking = next((tid for tid in subtree if not self.state.threads[tid].settled), None)
        if blocking is not None:
            rotor.record(
                "archive_skipped",
                {
                    "thread_id": msg.thread_id,
                    "blocking_thread_id": blocking,
                    "status": self.state.threads[blocking].status,
                },
            )
            return
        self._archive(subtree)

    def _archive(self, subtree: list[str]) -> None:
        for thread_id in reversed(subtree):  # deepest first
            record = self.state.threads[thread_id]
            record.archived = True
            if record.live:
                self.send(thread_id, messages.SetArchived(True))

    def _unarchive(self, record: ThreadRecord) -> None:
        if record.archived:
            record.archived = False
            self.send(record.thread_id, messages.SetArchived(False))

    # Delegation tree

    @rotor.on
    async def delegate_task(self, msg: messages.DelegateTask) -> None:
        s = self.state
        if self._task(msg.task_id) is not None:
            return
        source = self._live(msg.source_thread_id)
        if source is None:
            return
        siblings = len(self._children(source.thread_id))
        limits = environment.Environment.current().config.thread
        if s.retiring:
            reason = "the agent is retiring"
        elif source.depth >= limits.max_delegation_depth:
            reason = f"delegation depth limit ({limits.max_delegation_depth}) reached"
        elif siblings >= limits.max_delegations_per_thread:
            reason = f"delegation fan-out limit ({limits.max_delegations_per_thread}) reached"
        else:
            reason = ""
        if reason:
            self.send(
                source.thread_id, messages.DelegationRejected(msg.task_id, msg.objective, reason)
            )
            rotor.record("delegation_rejected", {"task_id": msg.task_id, "reason": reason})
            return
        child = ThreadRecord(
            task_id=msg.task_id,
            task_handle=msg.task_handle or f"task-{siblings + 1}",
            parent_thread_id=source.thread_id,
            depth=source.depth + 1,
            upstream=source.branch,
            objective=msg.objective,
            repository=msg.repository,
            repository_branch=msg.branch,
        )
        text = msg.objective
        if msg.repository:
            branch = f"\nBranch: {msg.branch}" if msg.branch else ""
            text = f"Repository: {msg.repository}{branch}\n\n{msg.objective}"
        self._spawn_thread(
            child,
            prompts=[{"text": text, "request_id": "", "source": "parent"}],
            upstream_sha=msg.upstream_sha,
        )

    @rotor.on
    async def message_parent(self, msg: messages.MessageParent) -> None:
        record = self._task(msg.task_id)
        if record is None or record.thread_id != msg.thread_id or record.task_status == "cancelled":
            return
        parent = self._live(record.parent_thread_id) if record.parent_thread_id else None
        if parent is None:
            return
        self.send(
            parent.thread_id,
            messages.TaskMessageInput(
                record.task_id,
                record.thread_id,
                record.objective,
                _task_status(record.task_status),
                msg.text,
                record.task_handle,
            ),
        )
        rotor.record("task_message", {"task_id": record.task_id, "thread_id": record.thread_id})

    @rotor.on
    async def task_completed(self, msg: messages.TaskCompleted) -> None:
        record = self._task(msg.task_id)
        if (
            record is None
            or record.thread_id != msg.thread_id
            or record.task_status == "cancelled"
            or msg.status not in ("working", "completed")
        ):
            return
        record.task_status = _task_status(msg.status)
        completed = record.task_status == "completed"
        record.completion_summary = msg.summary if completed else ""
        record.completion_result = msg.result if completed else ""
        record.deliverables = list(msg.deliverables) if completed else []
        record.absorb_proposals(msg.proposals)
        parent = self._live(record.parent_thread_id) if record.parent_thread_id else None
        if parent is None:
            return
        self.send(
            parent.thread_id,
            messages.TaskCompletionInput(
                record.task_id,
                record.thread_id,
                record.objective,
                record.task_status,
                msg.summary,
                msg.result,
                list(msg.deliverables),
                list(record.proposals),
                self._child_roster(record.parent_thread_id),
                record.task_handle,
            ),
        )
        rotor.record("task_completed", {"task_id": record.task_id, "thread_id": record.thread_id})

    @rotor.on
    async def message_task(self, msg: messages.MessageTask) -> None:
        record = self._task_for_action(msg.task_id, msg.source_thread_id, "message")
        if record is None:
            return
        self._prompt_from_parent(record, msg.text)
        self._accept(msg.source_thread_id, record, "message")

    @rotor.on
    async def cancel_task(self, msg: messages.CancelTask) -> None:
        record = self._task_for_action(msg.task_id, msg.source_thread_id, "cancel")
        if record is None:
            return
        for thread_id in self._subtree(record.thread_id):
            member = self.state.threads[thread_id]
            member.task_status = "cancelled"
            member.cancel_reason = msg.reason
            if member.live:
                self.send(thread_id, messages.CancelAssignedTask(msg.reason))
        self._accept(msg.source_thread_id, record, "cancel")

    @rotor.on
    async def archive_task(self, msg: messages.ArchiveTask) -> None:
        record = self._task_for_action(msg.task_id, msg.source_thread_id, "archive")
        if record is None:
            return
        subtree = self._subtree(record.thread_id)
        blocker = self._archive_blocker(subtree)
        if blocker is not None:
            self._reject(msg.source_thread_id, msg.task_id, "archive", *blocker)
            return
        self._archive(subtree)
        self._accept(msg.source_thread_id, record, "archive", subtree)

    def _archive_blocker(self, subtree: list[str]) -> tuple[str, str] | None:
        """Why a parent may not archive this subtree yet, as a (code, reason) pair."""
        for thread_id in subtree:
            t = self.state.threads[thread_id]
            if not t.settled:
                return "subtree_active", f"thread {thread_id} is {t.status}"
            if t.task_status not in FINISHED:
                return "unfinished_task", f"task {t.task_id} is {t.task_status}"
            if t.task_status != "cancelled" and any(not p.get("merged") for p in t.proposals):
                return "proposal_pending", f"task {t.task_id} has an unresolved proposal"
        return None

    @rotor.on
    async def review_task_proposal(self, msg: messages.ReviewTaskProposal) -> None:
        """Approvals run inside the parent thread; only rejections route through here."""
        record = self._task_for_action(msg.task_id, msg.source_thread_id, "review")
        if record is None:
            return
        if msg.decision != "reject" or not msg.comment.strip():
            self._reject(
                msg.source_thread_id,
                msg.task_id,
                "review",
                "invalid_action",
                "a rejection needs a comment",
            )
            return
        self._prompt_from_parent(
            record, f"Your {msg.section} proposal was rejected:\n{msg.comment}"
        )

    @rotor.on
    async def proposal_merged(self, msg: messages.ProposalMerged) -> None:
        record = self._task(msg.task_id)
        if record is None or record.parent_thread_id != msg.source_thread_id:
            return
        record.mark_merged(msg.branch, msg.proposal_sha, msg.merge_sha)
        self.send(
            record.thread_id,
            messages.ProposalAccepted(msg.branch, msg.proposal_sha, msg.merge_sha),
        )

    def _task_for_action(
        self, task_id: str, source_thread_id: str, action: str
    ) -> ThreadRecord | None:
        """The direct child task a thread may act on, or None after a structured rejection."""
        record = self._task(task_id)
        if record is None:
            self._reject(source_thread_id, task_id, action, "unknown_task", "no task has that id")
            return None
        if record.parent_thread_id != source_thread_id:
            self._reject(
                source_thread_id,
                task_id,
                action,
                "not_direct_parent",
                f"task belongs to direct parent thread {record.parent_thread_id}",
            )
            return None
        if record.task_status == "cancelled" and action != "archive":
            self._reject(source_thread_id, task_id, action, "cancelled", "task is cancelled")
            return None
        return record

    def _accept(
        self,
        source_thread_id: str,
        record: ThreadRecord,
        action: str,
        thread_ids: list[str] | None = None,
    ) -> None:
        if self._live(source_thread_id) is not None:
            self.send(
                source_thread_id,
                messages.TaskActionAccepted(
                    record.task_id,
                    action,
                    "working" if action == "message" else record.task_status,
                    list(thread_ids or []),
                ),
            )

    def _reject(
        self, source_thread_id: str, task_id: str, action: str, code: str, reason: str
    ) -> None:
        if self._live(source_thread_id) is not None:
            self.send(
                source_thread_id, messages.TaskActionRejected(task_id, action, code, reason)
            )
        rotor.record(
            "task_action_rejected",
            {"task_id": task_id, "action": action, "code": code, "reason": reason},
        )

    def _prompt_from_parent(self, record: ThreadRecord, text: str) -> None:
        self._unarchive(record)
        self.send(record.thread_id, messages.Prompt(text, "", source="parent"))

    # Budget

    def _roll_day(self) -> None:
        now = environment.Environment.current().now()
        self.state.budget.roll(now)
        self.schedule(
            messages.NewDay(), delay=self.state.budget.next_day_at() - now, key="new-day"
        )

    @rotor.on
    async def admit(self, msg: messages.Admit) -> None:
        """Admission is pulled: a thread asks before each model call and waits for the reply."""
        s = self.state
        if self._live(msg.thread_id) is None or s.retiring:
            return
        self._roll_day()
        if not s.budget.exhausted:
            self.send(msg.thread_id, messages.Admitted(msg.epoch))
            return
        if not s.waiting:
            rotor.record("budget_exhausted", s.budget.view())
        s.waiting[msg.thread_id] = msg.epoch
        self.send(msg.thread_id, messages.Held(msg.epoch))

    def _admit_waiting(self) -> None:
        s = self.state
        if s.budget.exhausted:
            return
        for thread_id, epoch in s.waiting.items():
            self.send(thread_id, messages.Admitted(epoch))
        s.waiting.clear()

    @rotor.on
    async def spent(self, msg: messages.Spent) -> None:
        self._roll_day()
        self.state.budget.spend(msg.tokens)

    @rotor.on
    async def grant(self, msg: messages.Grant) -> None:
        self._roll_day()
        if self.state.budget.grant(msg.id, msg.amount):
            rotor.record("budget_grant", {"id": msg.id, "amount": msg.amount})
            self._admit_waiting()

    @rotor.on
    async def new_day(self, msg: messages.NewDay) -> None:
        self._roll_day()
        self._admit_waiting()

    @rotor.on
    async def secret_requested(self, msg: messages.SecretRequested) -> None:
        self.state.secret_requests[msg.name] = msg.note

    @rotor.on
    async def secret_provided(self, msg: messages.SecretProvided) -> None:
        self.state.secret_requests.pop(msg.name, None)

    # Supervision

    @rotor.on(thread.AgentThread.Done)
    async def thread_done(self, msg: rotor.ChildDone) -> None:
        record = self.state.threads[msg.ref.id]
        record.live = False
        self.state.waiting.pop(msg.ref.id, None)
        self._maybe_stop()

    @rotor.on(thread.AgentThread.Failed)
    async def thread_failed(self, msg: rotor.ChildFailed) -> None:
        record = self.state.threads[msg.ref.id]
        self.state.waiting.pop(msg.ref.id, None)
        if record.live:
            record.live = False
            if isinstance(msg.reason, rotor.Cancelled):
                record.status = "retired"
            else:
                record.status, record.error = "failed", str(msg.reason)
                parent = self._live(record.parent_thread_id) if record.parent_thread_id else None
                if parent is not None and record.task_status != "cancelled":
                    self.send(
                        parent.thread_id,
                        messages.TaskMessageInput(
                            record.task_id,
                            record.thread_id,
                            record.objective,
                            record.task_status,
                            f"Thread failed: {record.error}",
                            record.task_handle,
                        ),
                    )
            record.cleanup = "pending"
            # A cancelled child may still have a command running remotely: wait out
            # the longest selectable timeout before exporting and stopping its sandbox.
            delay = config.MAX_COMMAND_TIMEOUT_SECONDS + 5
            self.schedule(
                messages.Cleanup(msg.ref.id), delay=delay, key=f"cleanup:{msg.ref.id}"
            )
        self._maybe_stop()

    @rotor.on
    async def cleanup(self, msg: messages.Cleanup) -> None:
        record = self.state.threads[msg.thread_id]
        if record.cleanup != "pending":
            return
        record.cleanup = "running"
        self.spawn(
            tools.cleanup_sandbox,
            input={
                "owner": self.state.agent_id,
                "branch": record.branch,
                "sandbox": record.sandbox,
                "chat": {"id": record.chat_id},
                "process_id": msg.thread_id,
                "operation_id": f"cleanup:{msg.thread_id}",
                "upstream": record.upstream,
                "checkpoint_sha": record.checkpoint_sha,
            },
            key=f"cleanup:{msg.thread_id}",
        )

    @rotor.on(tools.cleanup_sandbox.Done)
    async def cleanup_done(self, msg: rotor.ChildDone) -> None:
        record = self.state.threads[msg.key.removeprefix("cleanup:")]
        record.cleanup = "complete"
        if isinstance(msg.output, str):
            record.checkpoint_sha = msg.output
        self._maybe_stop()

    @rotor.on(tools.cleanup_sandbox.Failed)
    async def cleanup_failed(self, msg: rotor.ChildFailed) -> None:
        record = self.state.threads[msg.key.removeprefix("cleanup:")]
        record.cleanup, record.error = "failed", str(msg.reason)
        rotor.record("cleanup_failed", {"key": msg.key, "reason": str(msg.reason)})
        self._maybe_stop()

    @rotor.on
    async def retire(self, msg: messages.Retire) -> None:
        """Retirement cancels every live thread; supervisor cleanup exports and stops."""
        self.state.retiring = True
        self.state.waiting.clear()
        self.unschedule("new-day")
        for record in self.state.threads.values():
            if record.live:
                self.cancel(record.thread_id)
        self._maybe_stop()

    def _maybe_stop(self) -> None:
        if self.state.retiring and not any(t.busy for t in self.state.threads.values()):
            self.stop({"agent_id": self.state.agent_id, "threads": len(self.state.threads)})

    @rotor.on
    async def handling_failed(self, msg: rotor.HandlingFailed) -> None:
        log.error(
            "supervisor %s handler failed with %s: %s",
            self.ref.id,
            msg.error.type,
            msg.error.detail[:1000],
        )
        rotor.record("agent_error", {"error": msg.error.detail})

    # Projections

    @rotor.query
    def roster(self) -> dict[str, Any]:
        s = self.state
        return {
            "agent_id": s.agent_id,
            "retiring": s.retiring,
            "waiting": sorted(s.waiting),
            "budget": s.budget.view(),
            "threads": [self._thread_view(t) for t in s.threads.values()],
            "tasks": {t.task_id: self._task_view(t) for t in s.threads.values()},
            "requests": dict(s.requests),
            "secret_requests": dict(s.secret_requests),
        }

    def _thread_view(self, record: ThreadRecord) -> dict[str, Any]:
        view = dataclasses.asdict(record)
        view["task_status"] = _task_status(record.task_status)
        if record.thread_id in self.state.waiting:
            view["status"] = "waiting"
        return view

    def _task_view(self, record: ThreadRecord) -> dict[str, Any]:
        return {
            "task_id": record.task_id,
            "handle": record.task_handle,
            "thread_id": record.thread_id,
            "chat_id": record.chat_id,
            "parent_thread_id": record.parent_thread_id,
            "objective": record.objective,
            "repository": record.repository,
            "branch": record.repository_branch,
            "depth": record.depth,
            "status": _task_status(record.task_status),
            "summary": record.completion_summary,
            "result": record.completion_result,
            "deliverables": list(record.deliverables),
            "proposals": list(record.proposals),
            "cancel_reason": record.cancel_reason,
        }


PROCESSES: list[type[rotor.DurableProcess[Any]]] = [
    Supervisor,
    thread.AgentThread,
    tools.run_bash,
    tools.run_skill_view,
    tools.run_hatchery_tool,
    tools.run_review,
    tools.consolidate,
    tools.cleanup_sandbox,
    thread.register_thread_turn,
    thread.announce_thread_turn,
    thread.finish_turn,
]


# Ingress


async def _binding(chat_id: str) -> dict[str, Any] | None:
    from hatchery.store import events

    return await events.tail(chat_id, "thread")


async def thread_for_chat(chat_id: str) -> str | None:
    """The thread process of a chat, once its first turn went out."""
    binding = await _binding(chat_id)
    return str(binding["thread_id"]) if binding else None


async def start_turn(
    chat_id: str,
    origin: typing.Literal["ui", "channel", "worker", "cron", "api", "schedule"],
    task_id: str | None = None,
    turn_id: str | None = None,
    actor_user_id: str | None = None,
):
    """Send one turn with the chat's new messages to the agent's supervisor."""
    from hatchery.agent import runtime
    from hatchery.store import chats, events, turns

    chat = await chats.get(chat_id)
    if chat is None:
        raise ValueError("unknown chat")
    if chat.agent_id is None:
        raise ValueError("chat has no agent")
    resolved_turn_id = turn_id or f"turn_{uuid.uuid4().hex}"
    binding = await _binding(chat_id) or {}
    cursor = int(binding.get("cursor", -1))
    fresh: list[dict[str, Any]] = []
    last = cursor
    for index, data in await events.read(chat_id, "messages"):
        if index <= cursor:
            continue
        last = index
        if data.get("role") != "user":
            continue
        metadata = (data.get("provider_metadata") or {}).get("hatchery") or {}
        if metadata.get("origin") == "thread":
            continue  # the thread's own inputs, persisted for the transcript
        fresh.append(data)
    agent_id = str(binding.get("agent_id") or chat.agent_id)
    handle = await runtime.client.start(
        Supervisor, input={"agent_id": agent_id}, key=agent_id, scope=SCOPE
    )
    thread_id = str(binding.get("thread_id") or rotor.child_id(handle.id, root_key(chat_id)))
    payload = messages.TurnInput(
        chat_id=chat_id,
        turn_id=resolved_turn_id,
        origin=origin,
        messages=fresh,
        linked=bool(await chats.bindings(chat_id)),
        task_id=task_id,
        actor_user_id=actor_user_id,
    )
    async with turns.run(chat_id):
        await handle.send(payload, idempotency_key=resolved_turn_id)
        generation = await thread.register_turn(payload, thread_id)
        if last != cursor or not binding:
            await events.append(
                chat_id, "thread", {"agent_id": agent_id, "thread_id": thread_id, "cursor": last}
            )
    return turns.ActiveTurn(
        resolved_turn_id, thread_id, origin, task_id, generation, actor_user_id
    )


async def prompt(
    agent_id: str,
    text: str,
    *,
    key: str,
    thread: str | None,
    source: typing.Literal["api", "schedule"],
) -> str:
    """Land one handler or schedule `prompt()` effect as a normal turn of the agent.

    Agentmesh sends `Prompt` to the agent keyed by a conversation alias. Here the
    conversation key (`thread`, default: the route path or `schedule:<name>`) names a
    chat, so related events continue one thread. `key` fixes the message and turn IDs,
    so a retried effect adds nothing. Returns the chat ID.
    """
    from hatchery.store import chats, events, turns

    def digest(value: str) -> str:
        return hashlib.sha256(f"{agent_id}\0{source}\0{value}".encode()).hexdigest()

    conversation = thread or key
    chat_id = f"chat_{digest(f'thread:{conversation}')[:12]}"
    await chats.create_once(chat_id, agent_id, f"{source}: {conversation}"[:80], None, trigger=source)
    message = ai.user_message(text)
    message.id = f"{source}_{digest(f'key:{key}')[:32]}"
    message.provider_metadata = {"hatchery": {"origin": source, "author": source, "key": key}}
    async with turns.run(chat_id):
        if message.id not in {data.get("id") for _, data in await events.read(chat_id, "messages")}:
            await events.append(chat_id, "messages", message.model_dump(mode="json"))
            await events.append(chat_id, "ui", {"type": "messages.changed"})
        await start_turn(chat_id, source, turn_id=f"turn_{digest(f'turn:{key}')[:32]}")
    return chat_id


async def active_turn(chat_id: str):
    """Return a live turn and repair projections for a thread that ended."""
    from hatchery.agent import runtime
    from hatchery.store import turns

    active = await turns.active(chat_id)
    if active is None:
        return None
    try:
        snapshot = await runtime.client.snapshot(active.run_id)
    except rotor.ProcessNotFound:
        binding = await _binding(chat_id) or {}
        try:
            supervisor = await runtime.client.snapshot(
                process_id(str(binding.get("agent_id", "")))
            )
        except rotor.ProcessNotFound:
            supervisor = None
        if supervisor is not None and supervisor.phase != "terminal":
            return active  # the supervisor has not spawned the thread yet
        await turns.finish(chat_id, active.turn_id, active.run_id, "failed", "thread is missing")
        return None
    except Exception:
        log.exception("failed to inspect thread process %s", active.run_id)
        return active
    if snapshot.phase != "terminal":
        return active
    await turns.finish(
        chat_id,
        active.turn_id,
        active.run_id,
        "failed",
        str(snapshot.failure or "thread process terminated"),
    )
    return None


async def send(agent_id: str, msg: Any, *, idempotency_key: str | None = None) -> str:
    """Send an operator message (grant, retire, archive) to an agent's supervisor."""
    from hatchery.agent import runtime

    handle = await runtime.client.start(
        Supervisor, input={"agent_id": agent_id}, key=agent_id, scope=SCOPE
    )
    return await handle.send(msg, idempotency_key=idempotency_key)


async def roster(agent_id: str) -> dict[str, Any]:
    from hatchery.agent import runtime

    handle = await runtime.client.start(
        Supervisor, input={"agent_id": agent_id}, key=agent_id, scope=SCOPE
    )
    value, _ = await handle.query(Supervisor.roster)
    return typing.cast(dict[str, Any], value)

