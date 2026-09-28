# hatchery frontend

Vite React SPA using TanStack Router and shadcn/Base UI components. FastAPI serves it, so API, SSE, and WebSocket calls are same-origin. Try changes on a preview deployment (see [`docs/deployments.md`](../../docs/deployments.md)).

## Checks

```sh
pnpm test
pnpm lint
pnpm build
```

The production build is written to `../hatchery/static/` so the Python
package can serve and distribute it. Build it before running `uv build` in
`backend/`.
