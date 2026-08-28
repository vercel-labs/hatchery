# hatchery

agent deployed to cloud, running mostly unattended. reachable from slack,
github, and its own ui.

hatchery monitors repos, can respond to issues, pings on slack, or cron
schedule. the output artifacts include reports, notifications, issues, and prs.

1. deployed to vercel as two services (frontend + backend, see vercel.json)
2. fastapi backend; next.js frontend (stock shadcn on base-ui primitives)
3. dogfoods ai sdk for python, workflows, sandbox, connect

## answer style

be brief, use simple terse language, do not use jargon. this helps with efficiency of communication.
do not overcomplicate. this is a test application, it should prioritize clarity.

## code guidelines

1. in python, import by module (unless it's `typing`) to improve namespacing and make it read to navigate code.
2. minimize the number of helper functions, prioritize locality of behavior.
3. keep apis as small as possible. keep public apis even smaller, try to shrink them to one function / object.
4. test file structure should mirror app's file structure, e.g. `agent/turn.py` -> `tests/agent/test_turn.py`. this helps project navigation a lot.
5. do not write tests that test mocks, codify broken behavior, repeat third-party library tests, or do typechecker's job.

## project setup

1. use uv to manage python (run inside `backend/`)
2. use pnpm to manage typescript (run inside `frontend/`)

slack/github webhooks only reach deployments (vercel connect); point them at
the current branch with `scripts/triggers.sh`.

## store

everything durable lives in backend/store/ (neon postgres via DATABASE_URL,
jsonl/json files under backend/.data without it — tests always use files).
the primitive is an append-only stream keyed by (stream_id, namespace),
seal's shape: a chat's transcript is its (chat_id, "messages") stream (one
model message per event, source of truth), its devbox record the tail of
(chat_id, "worker"). chats/spaces/bindings/dedupe are rows next to it.
schema is idempotent DDL, created on startup — no migrations.

## worker layer

the previous DevBox integration has been removed. the retained sandbox,
subagent, and tty boundaries are explicit not-implemented stubs while they are
rebuilt on Vercel Sandbox and Queues; see `migration.md`.

`.reference/api` is specification material for retained behavior only. excluded
DevBox subsystems must not be copied into hatchery.
