# Hatchery

Hatchery is a software factory. It coordinates coding agents in Vercel Sandboxes to monitor, investigate, modify code, open pull requests, and send notifications.

Work is organized into **agents**, which share repositories, reference material, and Git-backed memory across chats and scheduled jobs. Hatchery can be used from its web UI or through Slack and GitHub.

## How to use

1. Get into the allowlist
2. Go to https://hatchery.playground-vercel.tools/

Note that everybody from the allowlist can view and participate in everybody else's chats through any channel. Use your own judgement when choosing what kind of work to do there.

## How it works

- **Agents** are shared and named, like `hatchery` (the default one, created on first use). Each has an ID (also its Git folder and web address), a display name, repos, and settings in the database.
- **Git storage.** Each agent's files live in the storage repo under `agents/<id>/`: `AGENTS.md` (who the agent is), `USER.md`, `MEMORY.md`, `memories/`, skills, scripts, `api/` routes, and `schedules/` jobs. `wiki/` holds the team prompt (`wiki/PROMPT.md`) and team skills; it appears on `main` with the first reviewed wiki change. Creating an agent commits a starter folder. No secrets go into Git.
- **Threads.** A chat is a thread. It gets its own branch and sandbox before its first model call, loads its files from the branch, works with shell and file tools, and saves its changes back. Ordinary changes merge to `main` on their own; wiki and serving changes wait for review. A thread can hand work to child threads. Each agent has a daily token budget. Long histories are compacted. fx subagents and extra sandboxes work as before.
- **Serving.** Python handlers in `api/<route>/route.py` on `main` answer at `https://<agent>.<HATCHERY_SERVE_DOMAIN>/<route>`, in one sandbox per agent. A reviewed change goes live without a Hatchery deploy.
- **Secrets** are encrypted in the database, belong to one agent, and only reach that agent's serving sandbox. Agents can ask for a secret; people set, rotate, reveal, and delete them on the agent's API page.
- **Schedules.** `schedules/<name>/job.py` runs on a cron or an interval, and people can pause it. Prompt jobs (a saved prompt on a cron) also work as before.

See [`docs/arch/`](docs/arch) for details and [`docs/agentmesh-migration.md`](docs/agentmesh-migration.md) for the design.

Upgrading from 0.1 keeps the old data and queue topics. Nothing is dropped. Chats, Slack/GitHub links, prompt jobs, and sandbox records start fresh in new tables. Old chats and notes don't show up. See [`docs/arch/store.md`](docs/arch/store.md).

## Configuration

Set these on the Vercel project, in addition to the existing database, sign-in, and Connect variables:

| Variable | What it does |
| --- | --- |
| `HATCHERY_STORAGE_REPO` | Storage repo, `vercel-internal-playground/hatchery-storage`. Without it, agents have no Git files. |
| `GITHUB_CONNECTOR` | The Connect GitHub app (`github/hatchery`). Its installation needs read and write access (contents, pull requests) to the storage repo. `HATCHERY_GITHUB_INSTALLATION_ID` picks an installation; `GITHUB_TOKEN` is a fallback. |
| `HATCHERY_SERVE_DOMAIN` | Domain for agent web addresses. The project also needs the wildcard domain `*.<HATCHERY_SERVE_DOMAIN>` (a project setting, not `vercel.json`). |
| `HATCHERY_SECRETS_KEY` | 32-byte key (base64url) that encrypts agent secrets. Keep it stable: a new key can't read old secrets. |
| `HATCHERY_<SECTION>_<FIELD>` | Optional limits, defaulting to agentmesh's values: `MODEL` (`ID`, `MAX_OUTPUT_TOKENS`, `CONTEXT_WINDOW_TOKENS`), `THREAD` (`MAX_TURNS`, `BASH_CALLS_PER_TURN`, `COMMAND_TIMEOUT_SECONDS`, `SANDBOX_IDLE_SECONDS`, `COMPACT_ABOVE_TOKENS`, `KEEP_RECENT_MESSAGES`, `MAX_DELEGATION_DEPTH`, `MAX_DELEGATIONS_PER_THREAD`), `BUDGET` (`TOKENS_PER_DAY`), `REVIEW` (`WORKSPACE`, `SERVE`, `WIKI`: `auto` or `review`), `SERVE` (`REVISION_TTL_SECONDS`, `REQUEST_TIMEOUT_SECONDS`, `JOB_TIMEOUT_SECONDS`, `MAX_REQUEST_BYTES`). See `backend/hatchery/config.py`. |

## Development

Hatchery is developed and tested through Vercel preview deployments. Running the application locally is not supported.

Point Slack and GitHub triggers at the branch when testing integrations:

```sh
./scripts/triggers.sh
```

Use [`docs/use-agent-browser.md`](docs/use-agent-browser.md) for browser-driven testing and [`docs/use-braintrust.md`](docs/use-braintrust.md) to inspect agent runs.

The frontend is a Vite React SPA using TanStack Router and is served by FastAPI. The backend uses Rotor and the AI SDK for Python. Hatchery runs Rotor workers on Vercel Queues, uses Vercel Sandboxes for coding agents, Lakebase Postgres on Neon for storage, and Vercel Connect for Slack and GitHub integrations.

## Python distribution

The PyPI distribution is `vercel-hatchery` (imported as `hatchery`). Run `make build` or `make ci` from the repository root. Publishing uses GitHub Actions trusted publishing; see [`backend/README.md`](backend/README.md) for the one-time setup and release steps.
