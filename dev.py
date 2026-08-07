"""Local dev server: runs the same app as main.py under uvicorn.

The slack and github channels are vercel connect-only: inbound webhooks are
forwarded by connect to a deployment, so locally this only verifies the app
boots and the routes mount. Test slack/github on a preview deployment.

    uv run dev.py
    curl -s localhost:8000/
"""

import os

import uvicorn

import main

if __name__ == "__main__":
    print(f"channels: {', '.join(main.bot.channels)}")
    uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
