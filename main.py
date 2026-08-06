"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Echo handler for now — replaces with the porting agent later. Channels:
- /chat/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/e2e-bot")
- /chat/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
"""

import fastapi

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
