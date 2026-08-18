"""Local dev server: runs the same app as app.server under uvicorn.

The slack and github channels are vercel connect-only: inbound webhooks are
forwarded by connect to a deployment, so locally this only verifies the app
boots and the routes mount.

    uv run dev.py
    curl -s localhost:8000/api/health
"""

import os
import pathlib

import uvicorn

# `vercel env pull backend/.env.local` supplies VERCEL_OIDC_TOKEN (ai gateway
# auth) and friends; load it before the app imports anything that reads env.
env = pathlib.Path(__file__).parent / ".env.local"
if env.exists():
    for line in env.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip('"'))

from app import server  # noqa: E402

if __name__ == "__main__":
    print(f"channels: {', '.join(server.bot.channels)}")
    uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
