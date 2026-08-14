"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/fabricator")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /channels/v1/devbox  task-state webhooks from devboxd (deployed mode; stub)
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)
- /api/chats/{id}/tty  websocket proxy to the chat's devbox pty (adds auth)

State lives in the store (postgres via DATABASE_URL, local files without):
a chat's transcript is its (chat_id, "messages") stream, its devbox record
the (chat_id, "worker") tail. Slack/github inbound lands in its chat via
_StoreHub (dedupe, claim binding, append); no turn runs on inbound yet.
"""

import asyncio
import contextlib
import logging

import fastapi
import fastapi.middleware.cors
import fastapi.responses
import pydantic
import websockets

import ai
import channels
import models
import store
from agent import devbox, dispatcher
from channels import github, slack
from store import chats, events, spaces

log = logging.getLogger("app")


class _StoreHub:
    """Lands inbound messages in their chat: claim the binding, append the
    message. Dedupe is durable, so webhook replays drop across instances.
    No turn runs on inbound yet — the message waits in the chat for the UI."""

    async def dispatch(self, channel: str, inbound: channels.Inbound) -> None:
        space = None
        if inbound.repo:
            space = next((s for s in await spaces.list_all() if inbound.repo in s.repos), None)
        space = space or await spaces.default()
        title = inbound.title or inbound.text.strip().splitlines()[0][:80]
        chat, created = await chats.claim(
            f"{channel}:{inbound.token}", channel, space.id, title, inbound.state
        )
        await events.append(
            chat.id, "messages", ai.user_message(inbound.text).model_dump(mode="json")
        )
        log.info("inbound %s -> %s chat %s", channel, "new" if created else "existing", chat.id)

    async def dedupe(self, key: str) -> bool:
        return await chats.dedupe(key)


bot = channels.App(_StoreHub())
bot.add(slack.channel())
bot.add(github.channel())


@contextlib.asynccontextmanager
async def lifespan(_: fastapi.FastAPI):
    await store.ensure_ready()
    await spaces.default()
    yield


app = fastapi.FastAPI(title="fabricator", lifespan=lifespan)
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
    found = await spaces.list_all()
    return found or [await spaces.default()]


@app.get("/api/chats")
async def list_chats() -> list[models.Chat]:
    return await chats.list_all()


class CreateChatRequest(pydantic.BaseModel):
    space_id: str | None = None
    title: str = "new chat"


@app.post("/api/chats")
async def create_chat(request: CreateChatRequest) -> models.Chat:
    space_id = request.space_id or (await spaces.default()).id
    return await chats.create(space_id, request.title)


@app.get("/api/chats/{chat_id}/messages")
async def chat_messages(chat_id: str) -> list[ai.ui.ai_sdk.UIMessage]:
    """The stored transcript as UI messages, for the chat view to resume from."""
    return ai.ui.ai_sdk.to_ui_messages(await _transcript(chat_id))


class ChatRequest(pydantic.BaseModel):
    chat_id: str
    messages: list[ai.ui.ai_sdk.UIMessage]


@app.post("/api/chat")
async def chat(request: ChatRequest) -> fastapi.responses.StreamingResponse:
    """One dispatcher turn, streamed as an AI SDK UI message stream.

    The store is the history: incoming messages the stream doesn't have yet
    (normally just the new user message — ids survive the UI roundtrip) are
    appended before the run, the run's new messages after it.
    """
    incoming, _ = ai.ui.ai_sdk.to_messages(request.messages)
    stored = await _transcript(request.chat_id)
    known = {message.id for message in stored}
    for message in incoming:
        if message.id not in known:
            await events.append(request.chat_id, "messages", message.model_dump(mode="json"))
            stored.append(message)

    history = [ai.system_message(dispatcher.SYSTEM), *stored]
    record = await events.tail(request.chat_id, "worker") or {"id": request.chat_id}
    agent = dispatcher.agent_for(record)

    async def stream():
        async with agent.run(dispatcher.model(), history) as result:
            async for chunk in ai.ui.ai_sdk.to_sse(result):
                yield chunk
            seen = {message.id for message in history}
            for message in result.messages:
                if message.id not in seen:
                    await events.append(
                        request.chat_id, "messages", message.model_dump(mode="json")
                    )

    return fastapi.responses.StreamingResponse(
        stream(), headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS
    )


async def _transcript(chat_id: str) -> list[ai.messages.Message]:
    return [
        ai.messages.Message.model_validate(data)
        for _, data in await events.read(chat_id, "messages")
    ]


@app.websocket("/api/chats/{chat_id}/tty")
async def tty(ws: fastapi.WebSocket, chat_id: str) -> None:
    """Bridge the browser to the chat's devbox pty session.

    Exists because the box wants the bearer token (query param on /__tty)
    and the browser shouldn't hold it. Frames pass through verbatim in both
    directions; devboxd's own protocol (handshake/tty-output/…) does the rest.
    """
    record = await events.tail(chat_id, "worker") or {}
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
