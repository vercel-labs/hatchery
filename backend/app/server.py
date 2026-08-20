"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/fabricator")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /channels/v1/devbox  authenticated task-state webhooks from devboxd
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)
- /api/chats/{id}/tty  websocket proxy to the chat's devbox pty (adds auth)

State lives in the store (postgres via DATABASE_URL, local files without):
a chat's transcript is its (chat_id, "messages") stream, its devbox record
the (chat_id, "worker") tail. Slack/github inbound lands in its chat via
_StoreHub (dedupe, claim binding, append); no turn runs on inbound yet.
"""

import asyncio
import contextlib
import hmac
import json
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
import vercel.functions
from agent import devbox, dispatcher
from channels import github, slack
from store import chats, events, spaces

log = logging.getLogger("app")
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


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
        # The browser sends its whole UI transcript. Assistant/tool messages are
        # server-owned and their IDs may change in the UI round-trip, so accepting
        # them here duplicates tool results and corrupts the next model history.
        if message.role == "user" and message.id not in known:
            await events.append(request.chat_id, "messages", message.model_dump(mode="json"))
            stored.append(message)

    history = [ai.system_message(dispatcher.SYSTEM), *stored]
    record = await events.tail(request.chat_id, "worker") or {"id": request.chat_id}

    def observe_task(worker: dict, created: dict) -> None:
        if devbox.webhook_url() is None:
            _spawn(_watch_local_task(request.chat_id, worker, created))

    agent = dispatcher.agent_for(record, observe_task)

    async def stream():
        async with agent.run(dispatcher.model(), history) as result:
            async for chunk in ai.ui.ai_sdk.to_sse(result):
                yield chunk
            for message in result.messages[len(history) :]:
                await events.append(
                    request.chat_id, "messages", message.model_dump(mode="json")
                )

    return fastapi.responses.StreamingResponse(
        stream(), headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS
    )


async def _transcript(chat_id: str) -> list[ai.messages.Message]:
    stored = [
        ai.messages.Message.model_validate(data)
        for _, data in await events.read(chat_id, "messages")
    ]
    return _dedupe_tool_history(stored)


def _dedupe_tool_history(messages: list[ai.messages.Message]) -> list[ai.messages.Message]:
    """Drop duplicate tool parts left by the old UI transcript ingestion bug."""
    seen_calls = set()
    seen_results = set()
    repaired = []
    for message in messages:
        parts = []
        for part in message.parts:
            if isinstance(part, ai.messages.ToolCallPart):
                if part.tool_call_id in seen_calls:
                    continue
                seen_calls.add(part.tool_call_id)
            elif isinstance(part, ai.messages.ToolResultPart):
                if part.tool_call_id in seen_results or part.tool_call_id not in seen_calls:
                    continue
                seen_results.add(part.tool_call_id)
            parts.append(part)
        if parts:
            repaired.append(message if len(parts) == len(message.parts) else message.model_copy(update={"parts": parts}))
    return repaired


@app.websocket("/api/chats/{chat_id}/tty")
async def tty(ws: fastapi.WebSocket, chat_id: str) -> None:
    """Bridge the browser to the chat's devbox pty session.

    Exists because the box wants the bearer token (query param on /__tty)
    and the browser shouldn't hold it. Frames pass through verbatim in both
    directions; devboxd's own protocol (handshake/tty-output/…) does the rest.
    """
    record = await events.tail(chat_id, "worker") or {}
    await ws.accept()
    if not record.get("box") or not record.get("session_id"):
        await ws.close(code=4404, reason="no coder session for this chat")
        return
    if record.get("task_state") in devbox.TERMINAL_STATES:
        await ws.close(code=4409, reason=f"coder {record['task_state']}")
        return
    q = ws.query_params
    url = devbox.tty_url(
        record["box"]["url"],
        record["session_id"],
        q.get("offset", "0"),
        q.get("cols", "80"),
        q.get("rows", "24"),
    )
    try:
        # no max_size: the box replays the whole scrollback as one frame,
        # which can be many MB — the default 1MB cap kills the connection
        # right after the handshake, forever (the replay never shrinks).
        async with websockets.connect(url, max_size=None) as box:

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
        # A real task session 404s until the agent PTY starts. If the task has
        # already settled there will never be a session, so stop retrying.
        latest = await events.tail(chat_id, "worker") or record
        if latest.get("task_state") in devbox.TERMINAL_STATES:
            await ws.close(code=4409, reason=f"coder {latest['task_state']}")
        else:
            await ws.close(code=4404, reason="coder session not on the box yet")
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.post("/channels/v1/devbox")
async def devbox_webhook(body: dict, chat_id: str = "", secret: str = "") -> dict:
    """Persist and deliver a devbox task transition.

    Devbox delivery is at-most-once, so the durable api-devbox task remains the
    recovery source. seq makes retries and out-of-order transitions harmless.
    """
    if body.get("kind") != "taskStateChange" or not isinstance(body.get("taskStateChange"), dict):
        raise fastapi.HTTPException(400, "unsupported devbox event")
    change = body["taskStateChange"]
    task_id = str(change.get("taskId", ""))
    if not task_id:
        raise fastapi.HTTPException(400, "missing task id")

    record = await events.tail(chat_id, "worker") if chat_id else None
    if record is None:
        raise fastapi.HTTPException(404, "unknown task")
    expected = str(record.get("webhook_secret", ""))
    if not expected or not hmac.compare_digest(secret, expected):
        raise fastapi.HTTPException(401, "invalid webhook secret")
    if record.get("task_id") not in (None, task_id):
        raise fastapi.HTTPException(404, "unknown task")
    record["task_id"] = task_id

    seq = int(change.get("seq") or 0)
    state = str(change.get("state", ""))
    current_seq = int(record.get("webhook_seq") or 0)
    if seq < current_seq or (
        seq == current_seq
        and (state not in devbox.TERMINAL_STATES or record.get("completion_delivered"))
    ):
        return {"ok": True, "duplicate": True}

    result = change.get("result") if isinstance(change.get("result"), dict) else {}
    record["webhook_seq"] = seq
    record["task_state"] = state
    if result:
        record["result"] = result
    await events.append(chat_id, "worker", dict(record))

    if state not in devbox.TERMINAL_STATES:
        return {"ok": True}

    if not record.get("completion_recorded"):
        completion = (
            f'<coder_completion task_id="{task_id}" state="{state}">\n'
            f"{json.dumps(result, separators=(',', ':'))}\n"
            "</coder_completion>"
        )
        await events.append(chat_id, "messages", ai.user_message(completion).model_dump(mode="json"))
        record["completion_recorded"] = True
        await events.append(chat_id, "worker", dict(record))
    if not record.get("completion_delivered"):
        vercel.functions.wait_until(_finish_task(chat_id, record, state, result))
    return {"ok": True}


async def _watch_local_task(chat_id: str, record: dict, created: dict) -> None:
    """Observe local work after the chat response, then use the webhook finisher."""
    for attempt in (1, 2, 3):
        state = str(created.get("state", "pending"))
        summary = ""
        async for frame in devbox.watch(record["box"]["url"], created["task_id"]):
            body = (frame or {}).get("body") or {}
            if (event := body.get("assistantEvent")) and event.get("name") == "complete":
                summary = str((event.get("body") or {}).get("summary") or summary)
            if transition := body.get("stateTransition"):
                state = str(transition["to"])
                if state in devbox.TERMINAL_STATES:
                    break
        row = await devbox.get_task(created["task_id"])
        result = row.get("result") or {}
        if summary and not result.get("summary"):
            result["summary"] = summary
        if row.get("state") in devbox.TERMINAL_STATES:
            state = row["state"]
        if "executable file not found" not in str(result.get("error", "")) or attempt == 3:
            await _record_task_completion(chat_id, record, state, result)
            return
        await asyncio.sleep(20)
        created = await devbox.create_task(
            record["box"]["id"], record["set_id"], record["task_prompt"]
        )
        record["task_id"] = created["task_id"]
        record["session_id"] = created["session_id"]
        record["task_state"] = created["state"]
        await events.append(chat_id, "worker", dict(record))


async def _record_task_completion(chat_id: str, record: dict, state: str, result: dict) -> None:
    task_id = str(record["task_id"])
    completion = (
        f'<coder_completion task_id="{task_id}" state="{state}">\n'
        f"{json.dumps(result, separators=(',', ':'))}\n"
        "</coder_completion>"
    )
    await events.append(chat_id, "messages", ai.user_message(completion).model_dump(mode="json"))
    record["task_state"] = state
    record["result"] = result
    record["completion_recorded"] = True
    await events.append(chat_id, "worker", dict(record))
    await _finish_task(chat_id, record, state, result)


async def _finish_task(chat_id: str, record: dict, state: str, result: dict) -> None:
    """Finish a callback after its HTTP response: run and fan out the new turn."""
    latest = await events.tail(chat_id, "worker") or record
    if latest.get("completion_delivered"):
        return
    record = latest
    message = str(record.get("completion_message") or "")
    if not message:
        message = await _run_dispatcher_turn(chat_id, record)
        record["completion_message"] = message
        await events.append(chat_id, "worker", dict(record))
    artifact = next(
        (str(pr["url"]) for pr in result.get("prs") or [] if isinstance(pr, dict) and pr.get("url")),
        message,
    )
    await chats.finish(chat_id, "done" if state == "complete" else "failed", artifact)
    event = channels.event(channels.protocol.MESSAGE_COMPLETED, message=message)
    failures = []
    for binding in await chats.bindings(chat_id):
        channel = bot.channels.get(binding.channel)
        if channel is None:
            continue
        try:
            await channel.on_event(event, binding.state)
        except Exception as error:
            log.exception("devbox completion delivery failed: %s -> %s", record["task_id"], binding.channel)
            failures.append(f"{binding.channel}: {error}")
    record["completion_delivered"] = not failures
    if failures:
        record["delivery_errors"] = failures
    else:
        record.pop("delivery_errors", None)
    await events.append(chat_id, "worker", dict(record))


async def _run_dispatcher_turn(chat_id: str, record: dict) -> str:
    """Run the completion message as a new dispatcher turn and persist it."""
    stored = await _transcript(chat_id)
    history = [ai.system_message(dispatcher.SYSTEM), *stored]
    agent = dispatcher.agent_for(record)
    async with agent.run(dispatcher.model(), history) as result:
        async for _ in result:
            pass
        added = result.messages[len(history) :]
        for message in added:
            await events.append(chat_id, "messages", message.model_dump(mode="json"))
    return next(
        (message.text for message in reversed(added) if message.role == "assistant" and message.text),
        "coder completion recorded",
    )


def _task_completion(state: str, result: dict) -> str:
    parts = [str(result.get("summary") or result.get("error") or f"coder {state}")]
    parts += [str(pr["url"]) for pr in result.get("prs") or [] if isinstance(pr, dict) and pr.get("url")]
    return " ".join(parts)


# Keep the generic channel route after the concrete devbox callback. Starlette
# matches in registration order, so mounting it earlier would consume
# /channels/v1/devbox as an unknown channel before this module's route ran.
app.include_router(bot.router)
