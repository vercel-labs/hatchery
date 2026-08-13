# factory

a software factory: an agent that runs on the cloud, mostly unattended,
reachable from slack, github, and its own ui. people and the agent share
project-scoped chats; a conversation started in a slack thread or a github
issue is also visible (and answerable) in the ui, and vice versa.

grew out of the e2e-bot: vercel has python sdks (workflow, sandbox, connect,
blob, oidc) that mirror the javascript sdks and consume the same backend api.
this is the second iteration, restarted from a clean slate; only the channel
adapters survived the first poc.

1. deployed to vercel as two services (frontend + backend, see vercel.json)
2. fastapi backend; next.js frontend (stock shadcn on base-ui primitives)
3. dogfoods ai sdk for python, workflows, sandbox, connect

## layout

- `backend/app` — http surface: server (vercel entrypoint), health
- `backend/channels` — platform adapters (slack, github; vercel connect-only)
  and the App that mounts them; inbound messages land in a `Hub` protocol,
  which the store/agent side will implement later
- `frontend/` — next.js app, dummy page for now

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

    cd backend && uv run pytest          # tests
    cd backend && uv run dev.py          # api on :8000
    cd frontend && pnpm dev              # ui on :3000, /api proxied to :8000
    vercel dev                           # or both services behind one port

slack/github webhooks only reach deployments (vercel connect); point them at
the current branch with `scripts/triggers.sh`.
