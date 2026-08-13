# fabricator

agent deployed to cloud, running mostly unattended. reachable from slack,
github, and its own ui.

fabricator monitors repos, can respond to issues, pings on slack, or cron
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

## project setup

1. use uv to manage python (run inside `backend/`)
2. use pnpm to manage typescript (run inside `frontend/`)

slack/github webhooks only reach deployments (vercel connect); point them at
the current branch with `scripts/triggers.sh`.
