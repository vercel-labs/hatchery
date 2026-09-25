# Vercel Hatchery

Hatchery is an agent deployed to Vercel, reachable from its web UI, Slack, and GitHub. It requires Vercel services and credentials to run; installing the Python package alone does not configure a deployment.

## What a deployment needs

- Postgres (`DATABASE_URL`, `DATABASE_URL_UNPOOLED`) for users, agents, chats, and Rotor state. Tables are created on startup. Upgrading from 0.1 drops and alters nothing; see `docs/arch/store.md` for which tables are new.
- A Git storage repo for agent files (`HATCHERY_STORAGE_REPO`), reached through the Connect GitHub app (`GITHUB_CONNECTOR`).
- `HATCHERY_SECRETS_KEY` for agent secrets, and `HATCHERY_SERVE_DOMAIN` plus its wildcard domain for agent web addresses.
- Queue subscribers for `hatchery.agent.runtime` (topics `hatchery-dispatcher-v1`, `hatchery-dispatcher-maintenance-v1`) and `hatchery.app.server:worker_event` (topic `hatchery-worker-events-v1`), as in `pyproject.toml`. A wrapper project must declare the same topics. These are the same topics as 0.1.

See the repository's top-level README for what each setting does.

## Build and publish

From the repository root, run `make build` to build the frontend, wheel, and source archive, or `make ci` to also check the lockfile and run backend and frontend tests and lint. The frontend build writes into `hatchery/static/` and runs before `uv build`; both the wheel and source archive include the UI. Check that PyPI dependencies work in a clean install; the development lockfile is not installed with the package.

To publish, use the manual **Publish to PyPI** GitHub Actions workflow on `main`. It runs `make ci`, passes the built archives to a separate `pypi` environment job, and runs `make publish` there with uv trusted publishing. Publishing locally without GitHub's OIDC identity will fail; no PyPI API token is needed. The Makefile selects the version from `pyproject.toml`, so bump it before each release. A version already on PyPI cannot be uploaded again.

One-time setup:

1. In this GitHub repository, create an environment named `pypi` (Settings → Environments). Restrict deployments to `main` and consider requiring reviewer approval.
2. On PyPI, register a GitHub trusted publisher with owner `vercel-labs`, repository `hatchery`, workflow filename `publish.yml`, and environment `pypi`. For the first release of `vercel-hatchery`, use a [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) under your PyPI account's Publishing settings; for an existing project, use its [Publishing settings](https://docs.pypi.org/trusted-publishers/adding-a-publisher/). A pending publisher does not reserve the name.
3. Run the workflow manually from GitHub Actions, selecting `main`. The PyPI fields and workflow filename/environment must match exactly. No token or GitHub secret is required.

The Python package is imported as `hatchery`; the PyPI distribution is `vercel-hatchery`. For local development instructions, see the repository's top-level README and `frontend/README.md`.
