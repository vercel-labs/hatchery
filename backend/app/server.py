"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/fabricator")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /channels/v1/devbox  task-state webhooks from devboxd (deployed mode; stub)
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)
- /api/chats/{id}/tty  websocket proxy to the chat's devbox pty (adds auth)

Chat worker state is in-memory (CHATS) — an MVP, not a store. Slack/github
inbound still lands in _EchoHub only.
"""

import asyncio
import logging

import fastapi
import fastapi.middleware.cors
import fastapi.responses
import pydantic
import websockets

import ai
import channels
import models
from agent import devbox, dispatcher
from channels import github, slack

log = logging.getLogger("app")

# hardcoded stubs until the store lands
STUB_SPACES = [
    models.Space(
        id="spc_self",
        name="fabricator",
        goal="work on itself: respond to issues, ship prs to its own repo",
        about=(
            "# fabricator\n\n"
            "An agent deployed to the cloud, running mostly unattended. Reachable "
            "from slack, github, and this ui.\n\n"
            "## Goal\n\n"
            "Work on itself: respond to issues, ping on slack, and ship prs to its "
            "own repo.\n\n"
            "## How it runs\n\n"
            "- two vercel services: fastapi backend, next.js frontend\n"
            "- chats spawn from slack, github, cron, or the ui\n"
            "- each chat gets a sandbox with the space's repos cloned\n\n"
            "## Conventions\n\n"
            "Keep changes small and reviewable. Prefer a report over a pr when "
            "uncertain."
        ),
        repos=["anbuzin/fabricator"],
        resources=[
            models.Resource(
                title="ai sdk for python",
                url="https://vercel.com/docs/ai-sdk-python",
                kind="reference",
            ),
            models.Resource(
                title="deployment",
                url="https://fabricator.vercel.app",
            ),
        ],
        color="#38bdf8",
        created_at="2026-08-10T09:00:00+00:00",
    ),
    models.Space(
        id="spc_wfjs",
        name="workflows watch",
        goal="monitor workflows js, notify python team on change",
        about=(
            "# workflows watch\n\n"
            "Monitor `vercel/workflows` and keep the python team in the loop.\n\n"
            "## What to watch\n\n"
            "- api changes in the js sdk that the python port should mirror\n"
            "- changelog entries and breaking releases\n\n"
            "## Output\n\n"
            "A short slack digest per notable change; an issue when the python "
            "port needs work."
        ),
        repos=["vercel/workflows"],
        resources=[
            models.Resource(
                title="workflows changelog",
                url="https://github.com/vercel/workflows/releases",
                kind="reference",
            ),
        ],
        color="#fbbf24",
        created_at="2026-08-11T14:30:00+00:00",
    ),
]

STUB_CHATS = [
    models.Chat(
        id="chat_a1",
        space_id="spc_self",
        title="wire up the two-pane ui",
        trigger="ui",
        status="running",
        sandbox_id="sbx_9f2c",
        created_at="2026-08-13T10:05:00+00:00",
    ),
    models.Chat(
        id="chat_a2",
        space_id="spc_self",
        title="fix flaky slack channel test",
        trigger="slack:T024BE7LD",
        status="done",
        artifact="https://github.com/anbuzin/fabricator/pull/8",
        created_at="2026-08-12T16:40:00+00:00",
    ),
    models.Chat(
        id="chat_a3",
        space_id="spc_self",
        title="nightly repo sweep",
        trigger="cron",
        status="failed",
        artifact="sweep aborted: sandbox clone timed out",
        created_at="2026-08-13T03:00:00+00:00",
    ),
    models.Chat(
        id="chat_b1",
        space_id="spc_wfjs",
        title="weekly changelog digest",
        trigger="cron",
        status="queued",
        created_at="2026-08-13T08:00:00+00:00",
    ),
]


class _EchoHub:
    """Placeholder hub: logs inbound messages, dedupes in memory."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def dispatch(self, channel: str, inbound: channels.Inbound) -> None:
        log.info("inbound from %s token=%s: %s", channel, inbound.token, inbound.text)

    async def dedupe(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


bot = channels.App(_EchoHub())
bot.add(slack.channel())
bot.add(github.channel())

app = fastapi.FastAPI(title="fabricator")
app.include_router(bot.router)

# local dev: the ui talks to :8000 directly for streams — next's dev proxy
# severs quiet/long sse responses (and can't proxy websockets at all).
app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "channels": list(bot.channels)}


@app.get("/api/spaces")
async def list_spaces() -> list[models.Space]:
    return STUB_SPACES


@app.get("/api/chats")
async def list_chats() -> list[models.Chat]:
    return STUB_CHATS


# chat_id -> {"id", "box": {"id","url"}, "set_id", "task_id", "session_id"}
CHATS: dict[str, dict] = {}


class ChatRequest(pydantic.BaseModel):
    chat_id: str
    messages: list[ai.ui.ai_sdk.UIMessage]


@app.post("/api/chat")
async def chat(request: ChatRequest) -> fastapi.responses.StreamingResponse:
    """One dispatcher turn, streamed as an AI SDK UI message stream."""
    record = CHATS.setdefault(request.chat_id, {"id": request.chat_id})
    messages, _ = ai.ui.ai_sdk.to_messages(request.messages)
    messages = [ai.system_message(dispatcher.SYSTEM), *messages]
    agent = dispatcher.agent_for(record)

    async def stream():
        async with agent.run(dispatcher.model(), messages) as result:
            async for chunk in ai.ui.ai_sdk.to_sse(result):
                yield chunk

    return fastapi.responses.StreamingResponse(
        stream(), headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS
    )


@app.websocket("/api/chats/{chat_id}/tty")
async def tty(ws: fastapi.WebSocket, chat_id: str) -> None:
    """Bridge the browser to the chat's devbox pty session.

    Exists because the box wants the bearer token (query param on /__tty)
    and the browser shouldn't hold it. Frames pass through verbatim in both
    directions; devboxd's own protocol (handshake/tty-output/…) does the rest.
    """
    record = CHATS.get(chat_id) or {}
    if not record.get("box") or not record.get("session_id"):
        await ws.close(code=4404, reason="no coder session for this chat")
        return
    await ws.accept()
    q = ws.query_params
    url = record["box"]["url"].replace("https://", "wss://") + (
        f"/__tty?token={devbox.token()}&sessionId={record['session_id']}"
        f"&offset={q.get('offset', '0')}&cols={q.get('cols', '80')}&rows={q.get('rows', '24')}"
    )
    try:
        async with websockets.connect(url) as box:

            async def down():
                async for frame in box:
                    await ws.send_text(frame if isinstance(frame, str) else frame.decode())

            async def up():
                while True:
                    await box.send(await ws.receive_text())

            done, pending = await asyncio.wait(
                [asyncio.ensure_future(down()), asyncio.ensure_future(up())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            for d in done:  # retrieve, or the disconnect logs as an error
                d.exception()
    except (fastapi.WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except (OSError, websockets.InvalidHandshake):
        # the box 404s /__tty until the task's pty session actually starts
        # (and again after it errors) — close 4404 so the client retries.
        await ws.close(code=4404, reason="coder session not on the box yet")
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.post("/channels/v1/devbox")
async def devbox_webhook(body: dict) -> dict:
    """Task-state webhook receiver (deployed mode). Stub: log and ack."""
    kind = body.get("kind", "")
    log.info("devbox webhook %s: %s", kind, body.get(kind))
    return {"ok": True}
