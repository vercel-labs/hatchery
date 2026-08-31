# hatchery

See `AGENTS.md` for layout and development commands.

The worker layer always uses Vercel Sandbox and Queues. Local development uses
Vercel's local Queue broker through the same daemon, topics, protocol, and
subscriber as cloud.

## Local development

Expose `vercel dev` so cloud sandboxes can reach its Queue broker:

```sh
./scripts/reverse_proxy.sh
```

Keep that process open. In another terminal, run the commands it prints:

```sh
export HATCHERY_PUBLIC_URL='https://...vgrok...'
vercel dev
```

Open `http://localhost:3000`, create a chat, and ask the dispatcher to create a
sandbox and subagent. `vercel dev` supplies the local Queue endpoint and token;
Hatchery rewrites that endpoint to the public vgrok origin for the sandbox.

Deployments use hosted Vercel Queues through deployment OIDC. No local worker or
in-process task bypass exists. Sandbox and subagent terminals connect through the
backend WebSocket bridge to the authenticated in-sandbox daemon.

## Diagnostics

The terminal pane shows copyable sandbox, task, and fx session IDs. It also
formats commands for opening a new sandbox shell or reconnecting to an existing
subagent session:

```sh
uv run --project backend python backend/diagnostics.py sandbox shell \
  --url https://hatchery.example --chat chat_... --sandbox wrk_...
uv run --project backend python backend/diagnostics.py task attach \
  --url https://hatchery.example --chat chat_... --task task_...
```

Both commands require an interactive terminal. `sandbox shell` creates a new
manual bash session. `task attach` reconnects to the exact running task TTY.
