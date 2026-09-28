# hatchery

agent deployed to cloud, running mostly unattended. reachable from slack,
github, and its own ui.

hatchery monitors repos, can respond to issues, pings on slack, or cron
schedule. the output artifacts include reports, notifications, issues, and prs.

1. one fastapi service, shipped as the `vercel-hatchery` package and deployed by a wrapper repo (see `docs/deployments.md` and the deployments section below)
2. vite + tanstack router frontend, statically served by fastapi (stock shadcn on base-ui primitives)
3. dogfoods ai sdk for python, rotor, sandbox, connect

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
2. use pnpm to manage typescript (run inside `backend/frontend/`)

## deployments

`docs/deployments.md` explains the model. our instance:

- vercel project `hatchery`, team `vercel-internal-playground`. production: https://hatchery.playground-vercel.tools (also `hatchery-prod.playground-vercel.tools`).
- wrapper repo: `vercel-internal-playground/hatchery-storage` (internal). the project is git-connected to it; a push to its `main` deploys production. it is also the storage repo (`HATCHERY_STORAGE_REPO`).
- databases (neon, vercel-managed): `hatchery-db` for production, `hatchery-preview-db` for preview, shared by all previews and empty at start. no neon preview branching, no development environment.
- `CRON_SECRET`, `HATCHERY_APP_ORIGIN` are production only. `HATCHERY_STORAGE_REPO` is set for production and preview: agents cannot run a turn without it.

rules:

1. this repo deploys nothing. never run `vercel deploy` here: it breaks the wrapper's build cache.
2. to test a change, push the branch, then run `scripts/preview.sh <branch | pr number>` in a `hatchery-storage` checkout. `--dry-run` builds and tests only. if a build fails with "exceeds the maximum function size", rerun with `--no-cache`.
3. to release: publish to pypi from here, bump the pin and `uv lock` in `hatchery-storage`, preview, push its `main`.
4. never point preview at `hatchery-db`, or production at `hatchery-preview-db`.
5. slack/github webhooks only reach production. do not retarget the shared `github/hatchery` and `slack/hatchery` connect triggers.
6. check a deployment with `vercel inspect <url> --logs`, `vercel logs <url> --since 1h --json`, and `vercel curl /api/health --deployment <url>`, all with `--scope vercel-internal-playground`.

## how to verify and debug

1. use `docs/use-agent-browser.md` to access live deployments and run real agent sessions. production url above; previews print their url.
2. for completed runs, inspect telemetry traces in `docs/use-braintrust.md` to understand exactly what happened during the run. use `--profile "anbuzin's projects" --project braintrust-coffee-flame`.

