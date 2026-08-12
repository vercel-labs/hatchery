"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Say "parity" to the bot to run the durable parity workflow (scan + agent
report); anything else echoes. Channels:
- /chat/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/fabricator")
- /chat/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
"""

import fastapi
import vercel.workflow

import chat
from agent import worker
from chat.channels import github, slack


async def handler(turn: chat.Turn) -> None:
    if "parity" in turn.message.content.lower():
        await turn.status("scanning repos...")
        await vercel.workflow.start(
            worker.parity_workflow,
            {
                "channel": turn.channel,
                "state": dict(turn.session.channel_state),
            },
        )
        return
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
