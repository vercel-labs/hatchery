# hatchery

See `AGENTS.md` for layout and development commands.

The worker layer always uses Vercel Sandbox and Queues. Local development uses
Vercel's local Queue broker through the same daemon, topics, protocol, and
subscriber as cloud.

## Local development

Hatchery auth uses the same Vercel OAuth flow and Postgres tables locally and in
deployments. Set `DATABASE_URL`, `VERCEL_APP_CLIENT_ID`,
`VERCEL_APP_CLIENT_SECRET`, and `GITHUB_CONNECTOR`. Register
`http://localhost:3000/api/auth/callback` on the Vercel app. Set
`HATCHERY_APP_ORIGIN=http://localhost:3000` if the browser-facing origin cannot
be inferred from forwarded headers. See `auth.md` for the session, connection,
credential, and sandbox behavior.

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
in-process task bypass exists. Sandbox and subagent terminals require the browser
session before the backend bridges them to the authenticated in-sandbox daemon.

Scheduled jobs use five-field UTC cron expressions. Vercel calls `/api/cron` every
minute; set the same long random `CRON_SECRET` on the backend deployment so its
`Authorization: Bearer` header is accepted. Local heartbeat checks can use
`curl -H "Authorization: Bearer $CRON_SECRET" http://localhost:3000/api/cron`.
