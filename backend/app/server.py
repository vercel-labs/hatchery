"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

The backend service: ui api (app.api), channel webhooks (channels.App), and
the daily parity cron. Channels:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/e2e-bot")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG

Turns run as durable workflows (agent.turn); this process only appends the
user message and starts the run. The workflow worker (app.worker) does the
rest.
"""

import contextlib
import os
import typing

import fastapi
import fastapi.middleware.cors
import vercel.workflow

import channels
import store
from agent import turn
from agent.tasks import parity
from app import api
from channels import github, protocol, slack
from store import chats, projects


async def start_turn(chat: chats.Chat, text: str) -> None:
    await vercel.workflow.start(turn.run_turn, chat.id)


@contextlib.asynccontextmanager
async def lifespan(_app: fastapi.FastAPI) -> typing.AsyncIterator[None]:
    await store.ensure_ready()
    await projects.get_default()  # webhooks need somewhere to land
    yield


bot = channels.App(start_turn)
bot.add(slack.channel())
bot.add(github.channel())

app = fastapi.FastAPI(title="factory", lifespan=lifespan)
app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(bot.router)
app.include_router(api.router(bot))


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "channels": list(bot.channels)}


@app.get("/api/cron/parity")
async def cron_parity(request: fastapi.Request) -> dict:
    """Daily parity run (vercel cron; see crons in vercel.json).

    The run lives in a stable "daily parity" chat in the default project, so
    every report is visible (and answerable) in the UI like any other chat.
    """
    secret = os.environ.get("CRON_SECRET")
    if secret and request.headers.get("authorization") != f"Bearer {secret}":
        raise fastapi.HTTPException(401, "bad cron secret")
    project = await projects.get_default()
    chat, _created = await chats.claim("cron:parity", "cron", project.id, "daily parity", {})
    await chats.touch(chat.id)
    await bot.emit(chat.id, protocol.event(protocol.MESSAGE_RECEIVED, message="daily parity run", channel="cron"))
    await vercel.workflow.start(parity.parity_workflow, chat.id)
    return {"ok": True, "chat_id": chat.id}
