"""Local dev server for the chat module, with an echo handler.

The slack and github channels are vercel connect-only: inbound webhooks are
forwarded by connect to a deployment, so locally this only verifies the app
boots and the routes mount. Test slack/github on a preview deployment.

    uv run dev.py
    curl -s localhost:8000/
"""

import os

import fastapi
import uvicorn

import chat
from chat.channels import github, slack


async def handler(turn: chat.Turn) -> None:
    await turn.status("thinking...")
    await turn.reply(f"echo from {turn.channel} (turn {len(turn.session.history) // 2}): {turn.message.content}")


bot = chat.App(handler)
bot.add(slack.channel())
bot.add(github.channel())

app = fastapi.FastAPI()
app.include_router(bot.router)


@app.get("/")
async def health() -> dict:
    return {"ok": True, "channels": list(bot.channels)}


if __name__ == "__main__":
    print(f"channels: {', '.join(bot.channels)}")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
