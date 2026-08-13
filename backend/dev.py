"""Local dev server: runs the same app as app.server under uvicorn.

The slack and github channels are vercel connect-only: inbound webhooks are
forwarded by connect to a deployment, so locally this only verifies the app
boots and the routes mount.

    uv run dev.py
    curl -s localhost:8000/api/health
"""

import os

import uvicorn

from app import server

if __name__ == "__main__":
    print(f"channels: {', '.join(server.bot.channels)}")
    uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
