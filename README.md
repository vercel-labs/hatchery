# hatchery

See `AGENTS.md` for the repository layout and development commands.

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
