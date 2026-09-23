# hatchery frontend

Vite React SPA using TanStack Router and shadcn/Base UI components.

## Development

With the normal backend environment configured, run FastAPI in one terminal:

```sh
cd backend
uv run dev.py
```

Run Vite in another:

```sh
cd backend/frontend
pnpm dev
```

Open http://localhost:3000. During development the browser connects directly
to FastAPI at http://127.0.0.1:8000 so SSE and WebSockets are not proxied.
Override this with `VITE_BACKEND_ORIGIN` when needed.

## Checks

```sh
pnpm test
pnpm lint
pnpm build
```

The production build is written to `../hatchery/static/` so the Python
package can serve and distribute it. Build it before running `uv build` in
`backend/`.
