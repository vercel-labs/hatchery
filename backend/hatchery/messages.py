"""Durable messages: the operator protocol and the agent supervisor/thread protocol.

Ported from agentmesh `messages.py`. Rotor registers types by class name, so these
names are unique across Hatchery. Agentmesh's `github_subject` becomes
`actor_user_id`: coding credentials stay Hatchery's GitHub connection.
"""

import dataclasses
import typing

import rotor

# Operator -> supervisor / thread


@rotor.message
class TurnInput:
    """One Hatchery turn for a chat: the new chat messages since the thread's cursor.

    Every ingress (UI, Slack/GitHub, fx completion, prompt job) sends this to the
    agent supervisor, which routes it to the chat's thread. `messages` never repeats
    history the thread already has, so compacted history stays compacted.
    """

    chat_id: str
    turn_id: str
    origin: str  # ui | channel | worker | cron | thread
    messages: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    linked: bool = False
    task_id: str | None = None
    actor_user_id: str | None = None


@rotor.message
class Prompt:
    text: str
    request_id: str
    thread_id: str | None = None
    source: str = "operator"
    actor_user_id: str | None = None


@rotor.message
class Grant:
    """Extra tokens for the current UTC day; `id` deduplicates retries."""

    id: str
    amount: int


@rotor.message
class Resume:
    reason: str = "operator resume"


@rotor.message
class Stop:
    reason: str = "operator stop"


@rotor.message
class Retire:
    reason: str = "agent retired"


@rotor.message
class SecretRequested:
    name: str
    note: str


@rotor.message
class SecretProvided:
    name: str


@rotor.message
class ArchiveThread:
    thread_id: str
    archived: bool = True


@rotor.message
class SetArchived:
    archived: bool


@rotor.message
class DelegateTask:
    task_id: str
    source_thread_id: str
    objective: str
    repository: str = ""
    branch: str = ""
    upstream_sha: str = ""
    task_handle: str = ""


@rotor.message
class MessageParent:
    """One immutable child-to-parent message, routed through the supervisor."""

    task_id: str
    thread_id: str
    text: str


@rotor.message
class TaskMessageInput:
    task_id: str
    reporting_thread_id: str
    objective: str
    status: str
    text: str
    task_handle: str = ""


@rotor.message
class TaskCompleted:
    """A child's final result after its completion handoff has consolidated."""

    task_id: str
    thread_id: str
    status: str
    summary: str
    result: str
    deliverables: list[str] = dataclasses.field(default_factory=list)
    proposals: list[dict[str, object]] = dataclasses.field(default_factory=list)


@rotor.message
class TaskCompletionInput:
    task_id: str
    reporting_thread_id: str
    objective: str
    status: str
    summary: str
    result: str
    deliverables: list[str] = dataclasses.field(default_factory=list)
    proposals: list[dict[str, object]] = dataclasses.field(default_factory=list)
    roster: list[dict[str, object]] = dataclasses.field(default_factory=list)
    task_handle: str = ""


@rotor.message
class DelegationRejected:
    task_id: str
    objective: str
    reason: str


@rotor.message
class MessageTask:
    task_id: str
    source_thread_id: str
    text: str


@rotor.message
class CancelTask:
    task_id: str
    source_thread_id: str
    reason: str


@rotor.message
class ArchiveTask:
    task_id: str
    source_thread_id: str


@rotor.message
class TaskActionAccepted:
    task_id: str
    action: str
    status: str
    thread_ids: list[str] = dataclasses.field(default_factory=list)


@rotor.message
class TaskActionRejected:
    task_id: str
    action: str
    code: str
    reason: str


@rotor.message
class CancelAssignedTask:
    reason: str


@rotor.message
class ReviewTaskProposal:
    task_id: str
    source_thread_id: str
    section: str
    decision: str
    comment: str = ""


@rotor.message
class ProposalMerged:
    task_id: str
    source_thread_id: str
    branch: str
    proposal_sha: str
    merge_sha: str


@rotor.message
class ProposalAccepted:
    branch: str
    proposal_sha: str
    merge_sha: str


# Supervisor <-> thread


@rotor.message
class Spent:
    tokens: int


@rotor.message
class Admit:
    """A thread asks to spend budget on one model call (a turn or a compaction summary).

    `epoch` fences the reply: the thread advances it for every request, activation,
    and stop, so a late answer to an earlier request is ignored.
    """

    thread_id: str
    epoch: int


@rotor.message
class Admitted:
    epoch: int
    attempt: int = 0


@rotor.message
class Held:
    """The supervisor is holding an admission for budget; the thread may release its sandbox."""

    epoch: int


@rotor.message
@dataclasses.dataclass
class ThreadReport:
    """A thread's execution snapshot for the supervisor's roster; sent whenever it changes.

    Identity and tree position are not repeated here. The child owns assignment state;
    the supervisor mirrors it for routing and operator projections.
    """

    thread_id: str
    status: str
    summary: str = ""
    result: str = ""
    signals: list[dict[str, object]] = dataclasses.field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    ceiling: int = 0
    compactions: int = 0
    checkpoint_sha: str = ""
    base_sha: str = ""
    handoffs: int = 0
    archived: bool = False
    task_status: str = "working"
    completion_summary: str = ""
    completion_result: str = ""
    deliverables: list[str] = dataclasses.field(default_factory=list)
    proposals: list[dict[str, object]] = dataclasses.field(default_factory=list)
    error: str = ""


# Self-sent steps and timers


@rotor.message
class Step:
    pass


@rotor.message
class ReleaseSandbox:
    epoch: int


@rotor.message
class Signal:
    id: str
    note: str = ""


@rotor.message
class NewDay:
    pass


@rotor.message
class Cleanup:
    thread_id: str
