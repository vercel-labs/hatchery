"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/fabricator")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /channels/v1/devbox  authenticated task-state webhooks from devboxd
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)
- /api/chats/{id}/tty  websocket proxy to the chat's devbox pty (adds auth)

State lives in the store (postgres via DATABASE_URL, local files without):
a chat's transcript is its (chat_id, "messages") stream, its shared devbox
the (chat_id, "worker") tail, and coder launches are separate task rows.
Slack/github inbound lands in its chat via
_StoreHub (dedupe, claim binding, append); no turn runs on inbound yet.
"""

import asyncio
import contextlib
import hmac
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
from store import activity, chats, events, spaces, tasks

log = logging.getLogger("app")
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)

    def done(completed: asyncio.Task) -> None:
        _background.discard(completed)
        if not completed.cancelled() and (error := completed.exception()) is not None:
            log.error("background task failed", exc_info=error)

    task.add_done_callback(done)


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


class SupervisionOutcome(pydantic.BaseModel):
    notify: bool
    message: str | None = None


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

    def observe_task(launch: dict, created: dict) -> None:
        if devbox.webhook_url() is None:
            _spawn(_watch_local_task(request.chat_id, launch, created))
            _spawn(_supervise_local(launch["id"]))

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


@app.get("/api/chats/{chat_id}/tasks")
async def chat_tasks(chat_id: str) -> list[dict]:
    """Coder launches for the chat, oldest first for stable terminal tabs."""
    return [
        {
            key: record.get(key)
            for key in ("id", "title", "task_id", "session_id", "state", "created_at")
        }
        for record in await tasks.list_for_chat(chat_id)
    ]


@app.websocket("/api/chats/{chat_id}/tasks/{launch_id}/tty")
async def task_tty(ws: fastapi.WebSocket, chat_id: str, launch_id: str) -> None:
    """Bridge the browser to one task's durable devbox PTY session."""
    workspace = await events.tail(chat_id, "worker") or {}
    launch = await tasks.get(launch_id)
    await ws.accept()
    if launch is None or launch.get("chat_id") != chat_id:
        await ws.close(code=4404, reason="unknown coder task")
        return
    if not workspace.get("box") or not launch.get("session_id"):
        await ws.close(code=4404, reason="coder session not on the box yet")
        return
    q = ws.query_params
    url = devbox.tty_url(
        workspace["box"]["url"],
        launch["session_id"],
        q.get("offset", "0"),
        q.get("cols", "80"),
        q.get("rows", "24"),
    )
    try:
        # no max_size: the box may replay many MB of scrollback in one frame.
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
            for pending_task in pending:
                pending_task.cancel()
            for done_task in done:
                done_task.exception()
    except (fastapi.WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except (OSError, websockets.InvalidHandshake):
        await ws.close(code=4404, reason="coder session not on the box yet")
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.websocket("/api/chats/{chat_id}/tty")
async def tty(ws: fastapi.WebSocket, chat_id: str) -> None:
    """Compatibility route: attach to the chat's newest coder task."""
    launches = await tasks.list_for_chat(chat_id)
    if not launches:
        await ws.accept()
        await ws.close(code=4404, reason="no coder session for this chat")
        return
    await task_tty(ws, chat_id, launches[-1]["id"])


@app.post("/channels/v1/devbox")
async def devbox_webhook(body: dict, launch_id: str = "", secret: str = "") -> dict:
    """Persist one task event without disturbing sibling tasks."""
    kind = str(body.get("kind", ""))
    payload = body.get(kind)
    if kind not in ("taskStateChange", "assistantEvent") or not isinstance(payload, dict):
        raise fastapi.HTTPException(400, "unsupported devbox event")
    task_id = str(payload.get("taskId", ""))
    if not task_id:
        raise fastapi.HTTPException(400, "missing task id")

    record = await tasks.get(launch_id) if launch_id else None
    if record is None:
        raise fastapi.HTTPException(404, "unknown task")
    expected = str(record.get("webhook_secret", ""))
    if not expected or not hmac.compare_digest(secret, expected):
        raise fastapi.HTTPException(401, "invalid webhook secret")
    if record.get("task_id") not in (None, task_id):
        raise fastapi.HTTPException(404, "unknown task")
    record["task_id"] = task_id

    if kind == "assistantEvent":
        cursor = str(payload.get("ts", ""))
        event = payload.get("event")
        if not cursor or not isinstance(event, dict):
            raise fastapi.HTTPException(400, "invalid assistant event")
        if not await chats.dedupe(f"devbox:{launch_id}:activity:{cursor}"):
            return {"ok": True, "duplicate": True}
        await activity.append(record["id"], "assistant_event", event, source_cursor=cursor)
        return {"ok": True}

    seq = int(payload.get("seq") or 0)
    state = str(payload.get("state", ""))
    current_seq = int(record.get("webhook_seq") or 0)
    if seq < current_seq or (
        seq == current_seq
        and (state not in devbox.TERMINAL_STATES or record.get("completion_delivered"))
    ):
        return {"ok": True, "duplicate": True}
    if seq == current_seq:
        vercel.functions.wait_until(supervise_task(record["id"], "terminal"))
        return {"ok": True}

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    record["webhook_seq"] = seq
    record["state"] = state
    if result:
        record["result"] = result
    await activity.append(
        record["id"], "state_transition", {"state": state, "result": result, "seq": seq}
    )
    await tasks.save(record)
    if state not in devbox.TERMINAL_STATES:
        return {"ok": True}

    if not record.get("completion_delivered"):
        vercel.functions.wait_until(supervise_task(record["id"], "terminal"))
    return {"ok": True}


async def _supervise_local(launch_id: str) -> None:
    for delay in (60, 120):
        await asyncio.sleep(delay)
        if await supervise_task(launch_id, "periodic"):
            return
    while True:
        await asyncio.sleep(300)
        if await supervise_task(launch_id, "periodic"):
            return


async def _watch_local_task(chat_id: str, record: dict, created: dict) -> None:
    """Observe local work after the chat response, then use the same finisher."""
    workspace = await events.tail(chat_id, "worker") or {}
    for attempt in (1, 2, 3):
        state = str(created.get("state", "pending"))
        summary = ""
        async for frame in devbox.watch(workspace["box"]["url"], created["task_id"]):
            body = (frame or {}).get("body") or {}
            if event := body.get("assistantEvent"):
                cursor = str((frame or {}).get("ts", ""))
                if not cursor or await chats.dedupe(f"devbox:{record['id']}:activity:{cursor}"):
                    await activity.append(record["id"], "assistant_event", event, source_cursor=cursor or None)
                if event.get("name") == "complete":
                    summary = str((event.get("body") or {}).get("summary") or summary)
            if transition := body.get("stateTransition"):
                state = str(transition["to"])
                await activity.append(record["id"], "state_transition", dict(transition))
                if state in devbox.TERMINAL_STATES:
                    break
        row = await devbox.get_task(created["task_id"])
        result = row.get("result") or {}
        if summary and not result.get("summary"):
            result["summary"] = summary
        if row.get("state") in devbox.TERMINAL_STATES:
            state = row["state"]
        if "executable file not found" not in str(result.get("error", "")) or attempt == 3:
            record["state"] = state
            record["result"] = result
            await tasks.save(record)
            await supervise_task(record["id"], "terminal")
            return
        await asyncio.sleep(20)
        try:
            created = await devbox.create_task(
                workspace["box"]["id"], workspace["set_id"], record["prompt"]
            )
        except Exception as error:
            record["state"] = "errored"
            record["result"] = {"error": f"coder retry failed: {error}"}
            await activity.append(
                record["id"],
                "state_transition",
                {"from": state, "to": "errored", "error": str(error)},
            )
            await tasks.save(record)
            await supervise_task(record["id"], "terminal")
            return
        record["task_id"] = created["task_id"]
        record["session_id"] = created["session_id"]
        record["state"] = created["state"]
        await tasks.save(record)


async def supervise_task(launch_id: str, reason: str) -> bool:
    """Run one serialized status check. Return true when the task is terminal."""
    current = await tasks.get(launch_id)
    if current is None:
        return True
    terminal = current.get("state") in devbox.TERMINAL_STATES
    record = await tasks.claim_supervision(launch_id, terminal or reason == "terminal")
    if record is None:
        latest = await tasks.get(launch_id)
        return latest is None or latest.get("state") in devbox.TERMINAL_STATES

    generation = int(record["supervision_generation"])
    chat_id = record["chat_id"]
    cursor = int(record.get("supervision_cursor", -1))
    try:
        cached = str(record.get("completion_message") or "") if terminal else ""
        if cached:
            outcome = SupervisionOutcome(notify=True, message=cached)
        else:
            workspace = await events.tail(chat_id, "worker") or {"id": chat_id}
            wake = ai.user_message(
                f"Supervise coder {launch_id}. Call check_coder with launch_id={launch_id!r} "
                f"and after={cursor}. This is a {reason} check. Do not launch another coder. "
                "Return JSON with notify and message. For periodic checks, notify only for a meaningful "
                "milestone, attention request, failure, or completion. Terminal checks must notify."
            )
            outcome = await _run_supervision_turn(chat_id, workspace, wake)
            if terminal and outcome.message:
                record["completion_message"] = outcome.message
                await tasks.save(record)
        latest = await tasks.get(launch_id) or record
        if int(latest.get("supervision_generation") or 0) != generation:
            return latest.get("state") in devbox.TERMINAL_STATES
        terminal = latest.get("state") in devbox.TERMINAL_STATES
        updates: dict = {"supervision_cursor": await activity.cursor(launch_id)}
        if outcome.notify and outcome.message:
            failures = await _deliver(chat_id, outcome.message)
            updates["delivery_errors"] = failures
            if not failures and terminal:
                updates["completion_delivered"] = True
                updates["completion_message"] = outcome.message
        if terminal:
            result = latest.get("result") or {}
            artifact = next(
                (str(pr["url"]) for pr in result.get("prs") or [] if isinstance(pr, dict) and pr.get("url")),
                outcome.message or "coder completed",
            )
            siblings = await tasks.list_for_chat(chat_id)
            active = any(
                sibling["id"] != launch_id and sibling.get("state") not in devbox.TERMINAL_STATES
                for sibling in siblings
            )
            if not active:
                await chats.finish(
                    chat_id, "done" if latest.get("state") == "complete" else "failed", artifact
                )
        await tasks.finish_supervision(launch_id, generation, **updates)
        return terminal
    except Exception:
        await tasks.finish_supervision(launch_id, generation)
        raise


async def _deliver(chat_id: str, message: str) -> list[str]:
    failures = []
    event = channels.event(channels.protocol.MESSAGE_COMPLETED, message=message)
    for binding in await chats.bindings(chat_id):
        channel = bot.channels.get(binding.channel)
        if channel is None:
            continue
        try:
            await channel.on_event(event, binding.state)
        except Exception as error:
            log.exception("coder update delivery failed: %s -> %s", chat_id, binding.channel)
            failures.append(f"{binding.channel}: {error}")
    return failures


async def _run_supervision_turn(
    chat_id: str, record: dict, wake: ai.messages.Message
) -> SupervisionOutcome:
    stored = await _transcript(chat_id)
    history = [ai.system_message(dispatcher.SYSTEM), *stored, wake]
    agent = dispatcher.agent_for(record)
    async with agent.run(dispatcher.model(), history, output_type=SupervisionOutcome) as result:
        async for _ in result:
            pass
        outcome = result.output
        added = result.messages[len(history) :]
        for message in added[:-1]:
            await events.append(chat_id, "messages", message.model_dump(mode="json"))
        if outcome.notify and outcome.message:
            await events.append(
                chat_id,
                "messages",
                ai.assistant_message(outcome.message).model_dump(mode="json"),
            )
        return outcome


async def _run_dispatcher_turn(
    chat_id: str, record: dict, wake: ai.messages.Message | None = None
) -> str:
    """Run a dispatcher turn; wake context is model-only, never persisted."""
    stored = await _transcript(chat_id)
    history = [ai.system_message(dispatcher.SYSTEM), *stored]
    if wake is not None:
        history.append(wake)
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


# Keep the generic channel route after the concrete devbox callback. Starlette
# matches in registration order, so mounting it earlier would consume
# /channels/v1/devbox as an unknown channel before this module's route ran.
app.include_router(bot.router)
