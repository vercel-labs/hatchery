# Hatchery

Hatchery is a software factory. It coordinates coding agents in Vercel Sandboxes to monitor, investigate, modify code, open pull requests, and send notifications.

Work is organized into **spaces**, which share repositories, reference material, and notes across chats and scheduled jobs. Hatchery can be used from its web UI or through Slack and GitHub.

## How to use

1. Get into the allowlist
2. Go to https://hatchery.playground-vercel.tools/

Note that everybody from the allowlist can view and participate in everybody else's chats through any channel. Use your own judgement when choosing what kind of work to do there.

## Development

Hatchery is developed and tested through Vercel preview deployments. Running the application locally is not supported.

Point Slack and GitHub triggers at the branch when testing integrations:

```sh
./scripts/triggers.sh
```

Use [`docs/use-agent-browser.md`](docs/use-agent-browser.md) for browser-driven testing and [`docs/use-braintrust.md`](docs/use-braintrust.md) to inspect agent runs.

The frontend is a Vite React SPA using TanStack Router and is served by FastAPI. The backend uses Rotor and the AI SDK for Python. Hatchery runs Rotor workers on Vercel Queues, uses Vercel Sandboxes for coding agents, Lakebase Postgres on Neon for storage, and Vercel Connect for Slack and GitHub integrations.
