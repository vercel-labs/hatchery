# Store

Hatchery keeps two kinds of data:

- **Database** (`backend/hatchery/store/`, `backend/hatchery/worker/store.py`): users, sessions, connections, agents, chats, channel bindings, event streams, prompt jobs, and sandbox records. Neon Postgres when `DATABASE_URL` is set, or JSONL/JSON files under `backend/.data` otherwise. Tests always use files.
- **Git** (`backend/hatchery/workspace/`): each agent's files under `agents/<id>/` and the shared `wiki/` in the storage repo (`HATCHERY_STORAGE_REPO`). The canonical branch is `main`; threads work on their own branches and merge back.

Rotor keeps its own state (thread tree, budgets, secrets, schedules) in the same database, in its `rotor_*` tables.

The main database primitive is an append-only stream keyed by `(stream_id, namespace)`. A chat transcript is its `(chat_id, "messages")` stream, with one model message per event as the source of truth. Chats, agents, bindings, and dedupe records are rows alongside streams.

The schema is idempotent DDL created on startup. There are no migrations.

## Tables

| Table | Holds |
| --- | --- |
| `hatchery_users`, `hatchery_sessions`, `hatchery_oauth_states`, `hatchery_slack_identities`, `hatchery_github_identities` | Sign-in and connections |
| `hatchery_agents` | Agents (new in 0.2) |
| `hatchery_dedupe` | Webhook replay protection |
| `hatchery_chats_v2`, `hatchery_bindings_v2` | Chats and their Slack/GitHub links |
| `hatchery_streams`, `hatchery_events` | Event streams (transcripts, UI events, turns); `pg_notify` channel `hatchery_events` |
| `hatchery_jobs_v2`, `hatchery_job_executions_v2` | Prompt jobs and their runs |
| `hatchery_workers_v2`, `hatchery_worker_tasks`, `hatchery_worker_terminals` | Sandboxes, fx subagents, terminals |

## Upgrading from 0.1

Nothing is dropped or altered. A table keeps its 0.1 name when its schema is the same and
its old rows can't reach new code. It gets a `_v2` name only when that is not true:

- `hatchery_chats_v2`: the `space_id` column became `agent_id`, and old chats must not show up in lists.
- `hatchery_bindings_v2`: an old binding would route Slack/GitHub replies to a chat that no longer exists.
- `hatchery_jobs_v2`: the `space_id` column became `agent_id`, and old jobs must not run.
- `hatchery_workers_v2`: an old worker record would let a 0.1 sandbox's events through.
- `hatchery_job_executions_v2`: the 30-day cleanup of finished runs would delete 0.1 rows.

The rest keep their names. Streams and events are only read by stream ID, and new
stream IDs (chat and task IDs) are fresh random or derived IDs. Worker tasks and
terminals are only read by ID, chat, or worker, all fresh; events from unknown
workers are dropped before any task is touched.
