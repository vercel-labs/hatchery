# factory

a software factory: an agent that runs on the cloud, mostly unattended,
reachable from slack, github, and its own ui. people and the agent share
project-scoped chats; a conversation started in a slack thread or a github
issue is also visible (and answerable) in the ui, and vice versa.

grew out of the e2e-bot: vercel has python sdks (workflow, sandbox, connect,
blob, oidc) that mirror the javascript sdks and consume the same backend api.
the first factory task compares e2e tests across the two and reports what is
missing in python; it runs once a day via vercel cron.

1. deployed to vercel as two services (frontend + backend, see vercel.json)
2. fastapi backend; react/vite frontend
3. dogfoods ai sdk for python, workflows, sandbox, connect
4. postgres (neon) when DATABASE_URL is set, local files under backend/.data otherwise

## layout

- `backend/app` — http surface: server (entrypoint), ui api, workflow worker
- `backend/store` — durable state: projects (repos + memory), chats (+ channel
  bindings, dedupe), per-chat append-only event streams
- `backend/channels` — platform adapters (slack, github) and the App that
  routes webhooks into chats; the event protocol
- `backend/agent` — durable workflows: turn (one per user message) and
  tasks/parity (the daily scan + report agent)
- `frontend/` — project view (memory, repos, chats) and chat view (live
  stream tail)

everything durable is an event in the chat's stream; the ui tails it and
channel bindings are fed from the same appends, so every surface sees the
same conversation.

## answer style

be brief, use simple terse language, do not use jargon. this helps with efficiency of communication.
do not overcomplicate. this is a test application, it should prioritize clarity.

## code guidelines

1. in python, import by module (unless it's `typing`) to improve namespacing and make it read to navigate code.
2. minimize the number of helper functions, prioritize locality of behavior.
3. keep apis as small as possible. keep public apis even smaller, try to shrink them to one function / object.
4. test file structure should mirror app's file structure, e.g. `agent/turn.py` -> `tests/agent/test_turn.py`. this helps project navigation a lot.

## project setup

1. use uv to manage python (run inside `backend/`)
2. use pnpm to manage typescript (run inside `frontend/`)

## dev

    cd backend && uv run pytest          # tests (file-backed store, no db needed)
    cd backend && uv run dev.py          # api on :8000
    cd frontend && pnpm dev              # ui on :5173, /api proxied to :8000

slack/github webhooks only reach deployments (vercel connect); point them at
the current branch with `scripts/triggers.sh`.
