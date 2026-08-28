# hatchery

See `AGENTS.md` for layout and development commands.

The worker layer always uses Vercel Sandbox and Queues, including during local
development:

```sh
cd backend && uv run dev.py
cd frontend && pnpm dev
```

Open `http://localhost:3000`, create a chat, and ask the dispatcher to create a
sandbox and subagent. Local development needs the same Vercel credentials as the
deployment. TTY access is still not implemented.
