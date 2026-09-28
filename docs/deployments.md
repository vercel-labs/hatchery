# Deployments

This repo builds the `vercel-hatchery` Python package: a wheel with the app and the built UI. It does not deploy anything.

A Hatchery instance is deployed from a separate **wrapper repo**, connected to a Vercel project. The wrapper is thin:

- `app.py` imports Hatchery and exports its ASGI app.
- `workers.py` holds the Queue subscriber entrypoints (Vercel needs them in the deployment source).
- `pyproject.toml` and `uv.lock` pin `vercel-hatchery`.
- `vercel.json` routes every path to the app and runs the minute cron (`/api/cron`).

The wrapper can also be the storage repo for agent files (`agents/`, `wiki/`). Then it must skip deploys for storage-only commits and for `threads/**` and `consolidations/**` branches.

## Ship to production

1. Merge to `main` here.
2. Bump `version` in `backend/pyproject.toml` (PyPI never accepts a version twice) and run the **Publish to PyPI** workflow. See [`backend/README.md`](../backend/README.md).
3. In the wrapper, update the pin, run `uv lock`, and preview it.
4. Push the wrapper's production branch.

The first request through the production URL makes the new deployment the Rotor owner, so it runs the background work.

## Preview a branch

Push the branch here, then build a wheel from it, pin that wheel in a copy of the wrapper, and deploy the copy as a preview. Previews build from what is pushed, not from a local checkout.

## Preview database

- Previews need their own database, separate from production's.
- The first request to a preview makes it the Rotor owner. If previews share one database, the last preview used runs all background work in it, including work started from other previews.
- A preview never takes a database that production owns. If it is pointed at one, it logs `preview database is bound to another environment` and chat routes fail. Production is not affected.
- `POST /api/rotor/activate` with `Authorization: Bearer $ROTOR_RELEASE_SECRET` forces a takeover. Only use it on a preview database.
- A turn the agent cannot take fails in the chat with the error, and the agent recovers on its next turn once the cause is fixed. If branches with incompatible Rotor state leave the preview database stuck anyway, reset `hatchery-preview-db` from the Neon dashboard. It holds nothing worth keeping.

## Limits of previews

- Vercel runs crons only in production, so the heartbeat, schedules, and prompt jobs don't run on previews.
- Connect triggers for Slack and GitHub target a Git branch or a custom environment. A preview deployed from the CLI has no Git branch, so it doesn't receive their events.
- Previews need `HATCHERY_STORAGE_REPO` too. Without it, every turn fails with `no Environment installed`. Previews share the storage repo with production, so their thread branches land there.

## Don't

- Don't run `vercel deploy` from this repo. It builds this repo's layout into the wrapper's project, and the leftover build cache then breaks the next wrapper build with `Total bundle size ... exceeds the maximum function size`. Recover with one wrapper deploy without cache (`vercel deploy --force`).
- Don't share a database between production and previews.
- Don't retarget shared Connect triggers at a branch with no deployment. That cuts production off from Slack and GitHub.
