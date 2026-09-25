# Agent runtime

Each agent has one Rotor `Supervisor` (`backend/hatchery/agent/supervisor.py`,
key = agent id). It routes turns, owns the thread tree and the daily token
budget, and never calls models, Git, or sandboxes. Each chat has one
`AgentThread` (`backend/hatchery/agent/thread.py`), a child of the supervisor.
The thread is the durable AI SDK loop: it has a Git branch, a sandbox, and
shell/file/skill tools, plus Hatchery's sandbox, fx subagent, and channel tools.

A turn follows this path:

1. `supervisor.start_turn` sends a `TurnInput` with the chat's new user messages
   (after the cursor in the chat's `thread` events stream) to the supervisor,
   and registers the turn under the thread's process id.
2. The supervisor spawns the chat's thread on first use, or forwards the input.
3. `Step` asks the supervisor for admission, then `Admitted` runs one model call.
   Before the first call the thread acquires its sandbox, checkpoints dirty
   files, merges upstream, and loads `AGENTS.md`, memory, skills, and the wiki
   prompt from the branch.
4. Tool calls run one at a time in keyed children (`run_bash`, `run_skill_view`,
   `run_hatchery_tool`, `run_review`). Signals and task tools apply inline.
5. When the model replies without tools (or calls `idle`), `finish_turn`
   persists the turn's new messages to `(chat_id, "messages")`, mirrors replies
   to linked channels, completes fx worker bookkeeping, and records the turn's
   end. The thread then checkpoints and publishes the workspace and wiki.
6. After the idle grace period the sandbox is stopped (not destroyed), unless an
   fx task or terminal still uses it.

Delegated threads get their own chat (`trigger="task"`, `parent_chat_id`).
Compaction shortens only the thread's model history; the chat transcript stays
complete. Work the thread starts by itself (signals, task messages, grants,
resumes) runs under an internal turn with origin `thread`.

Rotor runs on Vercel Queues: topic `hatchery-dispatcher-v1`, maintenance topic
`hatchery-dispatcher-maintenance-v1`, consumer group `hatchery-dispatcher-v1`
(`backend/hatchery/agent/runtime.py`, subscribers in `backend/pyproject.toml`).
Sandbox daemons report on `hatchery-worker-events-v1` (consumer group
`hatchery-control-plane-v1`). These names are the same as in 0.1; only the
"dispatcher" in them is historical. Rotor only claims process and task types that
are registered now, so rows left by the 0.1 runtime in the shared `rotor_*`
tables are never loaded. `backend/rotor-schema.json` records the registered types
(`uv run python -m rotor.check hatchery.agent.runtime`).

A worker event is dropped unless its worker is in `hatchery_workers_v2` and owns
the event's task, so a sandbox started by 0.1 cannot wake a thread.

Serving and schedules have their own processes; see `serving.md`.

See `docs/agentmesh-migration.md` for the full design and its agentmesh sources.
