"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/hatchery")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /channels/v1/devbox  authenticated task-state webhooks from devboxd
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)
- /api/chats/{id}/subagents/{id}/tty  websocket proxy to a subagent pty

State lives in the store (postgres via DATABASE_URL, local files without):
a chat's transcript is its (chat_id, "messages") stream; devboxes and their
subagent launches are separate durable records owned by the chat.
Slack/github inbound lands in its chat via
_StoreHub (dedupe, claim binding, append); no turn runs on inbound yet.
"""

import asyncio
import contextlib
import hmac
import html
import json
import logging
import os
import re

import fastapi
import fastapi.middleware.cors
import fastapi.responses
import pydantic
import websockets

import ai
import ai.ui.ai_sdk.outbound_stream
import ai.ui.ai_sdk.ui_events
import channels
import models
import store
import vercel.functions
from agent import classifier, devbox, dispatcher, sandbox, topic
from channels import github, slack
from store import activity, chats, devboxes, events, spaces, subagents, terminals, turns

log = logging.getLogger("app")
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    if os.environ.get("VERCEL"):
        vercel.functions.wait_until(coro)
        return
    task = asyncio.create_task(coro)
    _background.add(task)

    def done(completed: asyncio.Task) -> None:
        _background.discard(completed)
        if not completed.cancelled() and (error := completed.exception()) is not None:
            log.error("background task failed", exc_info=error)

    task.add_done_callback(done)


class _StoreHub:
    """Land inbound messages in a chat and run one dispatcher turn.

    The channel endpoint already defers dispatch until after its fast ack, so
    this coroutine can own the full turn and its reply delivery.
    """

    async def dispatch(self, channel: str, inbound: channels.Inbound) -> None:
        found = await spaces.list_all() or [await spaces.default()]
        title = inbound.title or inbound.text.strip().splitlines()[0][:80]
        chat, created = await chats.claim(
            f"{channel}:{inbound.token}",
            channel,
            None,
            title,
            inbound.state,
        )
        if created:
            await events.append(
                chat.id, "messages", ai.user_message(inbound.text).model_dump(mode="json")
            )
            await _classify_chat(
                chat.id,
                inbound.text,
                {
                    "origin": channel,
                    "author": _inbound_author(inbound),
                    "repo": inbound.repo,
                    "channel_state": inbound.state,
                },
                found,
            )
            chat = await chats.get(chat.id) or chat
            _spawn(_name_chat(chat.id, inbound.text))
        elif chat.space_id is None:
            await _classify_chat(
                chat.id,
                inbound.text,
                {
                    "origin": channel,
                    "author": _inbound_author(inbound),
                    "repo": inbound.repo,
                    "channel_state": inbound.state,
                },
                found,
            )
            chat = await chats.get(chat.id) or chat
        else:
            await events.append(
                chat.id, "messages", ai.user_message(inbound.text).model_dump(mode="json")
            )
        log.info("inbound %s -> %s chat %s", channel, "new" if created else "existing", chat.id)
        await _run_inbound_turn(chat.id)

    async def dedupe(self, key: str) -> bool:
        return await chats.dedupe(key)


def _inbound_author(inbound: channels.Inbound) -> str:
    return str(
        inbound.state.get("user_id")
        or inbound.state.get("sender")
        or inbound.state.get("author")
        or "unknown"
    )


async def _name_chat(chat_id: str, prompt: str) -> None:
    generated = await topic.generate(prompt)
    if generated and await chats.set_topic(chat_id, generated):
        await events.append(chat_id, "ui", {"type": "chat.changed"})


async def _classify_chat(
    chat_id: str, prompt: str, metadata: dict, candidates: list[models.Space]
) -> models.Space:
    await _emit(chat_id, channels.event(channels.protocol.SPACE_ASSIGNING))
    selected = await classifier.classify(prompt, metadata, candidates)
    assigned = await chats.assign_space(chat_id, selected.id)
    if assigned is None:
        raise fastapi.HTTPException(404, "unknown chat")
    await _emit(
        chat_id,
        channels.event(
            channels.protocol.SPACE_ASSIGNED,
            space={"id": selected.id, "name": selected.name, "color": selected.color},
        ),
    )
    return selected


bot = channels.App(_StoreHub())
bot.add(slack.channel())
bot.add(github.channel())


@contextlib.asynccontextmanager
async def lifespan(_: fastapi.FastAPI):
    await store.ensure_ready()
    await spaces.default()
    yield


app = fastapi.FastAPI(title="hatchery", lifespan=lifespan)

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


class CreateSpaceRequest(pydantic.BaseModel):
    name: str

    @pydantic.field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        return name


@app.post("/api/spaces")
async def create_space(request: CreateSpaceRequest) -> models.Space:
    return await spaces.create(request.name)


@app.delete("/api/spaces/{space_id}", status_code=204)
async def delete_space(space_id: str) -> None:
    if any(chat.space_id == space_id for chat in await chats.list_all()):
        raise fastapi.HTTPException(409, "space still has chats")
    if not await spaces.delete(space_id):
        raise fastapi.HTTPException(404, "unknown space")


class UpdateSpaceRequest(pydantic.BaseModel):
    name: str
    about: str

    @pydantic.field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        return name


@app.patch("/api/spaces/{space_id}")
async def update_space(space_id: str, request: UpdateSpaceRequest) -> models.Space:
    space = await spaces.get(space_id)
    if space is None:
        raise fastapi.HTTPException(404, "unknown space")
    updated = models.Space.model_validate(
        {**space.model_dump(), "name": request.name, "about": request.about}
    )
    return await spaces.save(updated)


class UpdateSpaceResourcesRequest(pydantic.BaseModel):
    repos: list[str]
    resources: list[models.Resource]

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise ValueError("repos must use owner/repo form")
        return repos


@app.patch("/api/spaces/{space_id}/resources")
async def update_space_resources(
    space_id: str, request: UpdateSpaceResourcesRequest
) -> models.Space:
    space = await spaces.get(space_id)
    if space is None:
        raise fastapi.HTTPException(404, "unknown space")
    updated = models.Space.model_validate(
        {
            **space.model_dump(),
            "repos": request.repos,
            "resources": request.resources,
        }
    )
    return await spaces.save(updated)


@app.get("/api/chats")
async def list_chats() -> list[models.Chat]:
    found = await chats.list_all()
    for chat in found:
        if not chat.trigger.startswith("slack:") or chat.title.startswith("slack:"):
            continue
        title = re.sub(r"^<@[^>]+>\s*", "", chat.title)
        title = html.unescape(" ".join(title.split())).strip()
        chat.title = f"slack: {title[:53]}" if title else "slack: thread"
    return found


class CreateChatRequest(pydantic.BaseModel):
    title: str = "new chat"
    space_id: str | None = None


@app.post("/api/chats")
async def create_chat(request: CreateChatRequest) -> models.Chat:
    found = await spaces.list_all()
    if not found:
        found = [await spaces.default()]
    if request.space_id is not None and not any(space.id == request.space_id for space in found):
        raise fastapi.HTTPException(404, "unknown space")
    return await chats.create(request.space_id, request.title)


class AssignChatSpaceRequest(pydantic.BaseModel):
    space_id: str


@app.patch("/api/chats/{chat_id}/space")
async def assign_chat_space(chat_id: str, request: AssignChatSpaceRequest) -> models.Chat:
    if await spaces.get(request.space_id) is None:
        raise fastapi.HTTPException(404, "unknown space")
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    # TODO: add history for space changes
    return await chats.assign_space(chat_id, request.space_id)


@app.get("/api/chats/{chat_id}/events")
async def chat_events(
    chat_id: str, request: fastapi.Request, after: int = -1
) -> fastapi.responses.StreamingResponse:
    """Replay and stream durable UI invalidations for one chat."""
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    header = request.headers.get("last-event-id")
    cursor = max(after, int(header) if header and header.lstrip("-").isdigit() else -1)

    async def stream():
        watcher = events.watch(chat_id, "ui", cursor + 1)
        pending = asyncio.create_task(anext(watcher))
        try:
            while True:
                try:
                    index, event = await asyncio.wait_for(asyncio.shield(pending), timeout=30)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"id: {index}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                pending = asyncio.create_task(anext(watcher))
        finally:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
            await watcher.aclose()

    return fastapi.responses.StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chats/{chat_id}/messages")
async def chat_messages(chat_id: str) -> list[ai.ui.ai_sdk.UIMessage]:
    """The stored transcript as UI messages, with channel envelopes hidden."""
    messages = ai.ui.ai_sdk.to_ui_messages(await _transcript(chat_id))
    for message in messages:
        if message.role != "user":
            continue
        for part in message.parts:
            if getattr(part, "type", None) != "text":
                continue
            match = re.fullmatch(r'<slack_message\b[^>]*>\s*(.*?)\s*</slack_message>', part.text, re.DOTALL)
            if match is None:
                continue
            part.text = html.unescape(match.group(1))
            message.metadata = {**(message.metadata or {}), "origin": "slack"}
    return messages


class ChatRequest(pydantic.BaseModel):
    chat_id: str
    messages: list[ai.ui.ai_sdk.UIMessage]


class CompletionOutcome(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    notify: bool
    message: str | None


@app.post("/api/chat")
async def chat(request: ChatRequest) -> fastapi.responses.StreamingResponse:
    """One dispatcher turn, streamed as an AI SDK UI message stream.

    The store is the history: incoming messages the stream doesn't have yet
    (normally just the new user message — ids survive the UI roundtrip) are
    appended before the run, the run's new messages after it.
    """
    incoming, _ = ai.ui.ai_sdk.to_messages(request.messages)

    async def stream():
        async with turns.run(request.chat_id):
            stored = await _transcript(request.chat_id)
            known = {message.id for message in stored}
            received = []
            for message in incoming:
                # The browser sends its whole UI transcript. Assistant/tool messages are
                # server-owned and their IDs may change in the UI round-trip, so accepting
                # them here duplicates tool results and corrupts the next model history.
                if message.role == "user" and message.id not in known:
                    await events.append(request.chat_id, "messages", message.model_dump(mode="json"))
                    stored.append(message)
                    received.append(message)
            for message in received:
                await _emit(
                    request.chat_id,
                    channels.event(
                        channels.protocol.MESSAGE_RECEIVED,
                        message=message.text,
                        origin="ui",
                    ),
                )

            current = await chats.get(request.chat_id)
            if received and current is not None and current.topic is None:
                first = next((message for message in stored if message.role == "user"), None)
                if first is not None:
                    _spawn(_name_chat(request.chat_id, first.text))
            if current is None:
                raise fastapi.HTTPException(404, "unknown chat")
            if current.space_id is None:
                first = next((message for message in stored if message.role == "user"), None)
                if first is None:
                    raise fastapi.HTTPException(409, "chat has no first prompt")
                yield ai.ui.ai_sdk.outbound_stream.format_sse(
                    ai.ui.ai_sdk.ui_events.UIDataEvent(
                        data_type="space-assignment",
                        data={"state": "assigning"},
                    )
                )
                selected = await _classify_chat(
                    request.chat_id,
                    first.text,
                    {"origin": "ui", "author": "current user"},
                    await spaces.list_all() or [await spaces.default()],
                )
                yield ai.ui.ai_sdk.outbound_stream.format_sse(
                    ai.ui.ai_sdk.ui_events.UIDataEvent(
                        data_type="space-assignment",
                        data={
                            "state": "assigned",
                            "space_id": selected.id,
                            "space_name": selected.name,
                        },
                    )
                )
            space = await _space_for_chat(request.chat_id)
            history = [ai.system_message(dispatcher.system_prompt(space)), *stored]
            agent = dispatcher.agent_for({"id": request.chat_id})
            await _emit(request.chat_id, channels.event(channels.protocol.TURN_STARTED))
            try:
                async with agent.run(dispatcher.model(), history) as result:
                    async for chunk in ai.ui.ai_sdk.to_sse(result):
                        yield chunk
                    added = result.messages[len(history) :]
                    for message in added:
                        await events.append(
                            request.chat_id, "messages", message.model_dump(mode="json")
                        )
                reply = next(
                    (message.text for message in reversed(added) if message.role == "assistant" and message.text),
                    "",
                )
                if reply:
                    await _deliver(request.chat_id, reply)
            except Exception as error:
                await _emit(
                    request.chat_id,
                    channels.event(channels.protocol.TURN_FAILED, error=str(error)),
                )
                raise

    return fastapi.responses.StreamingResponse(
        stream(), headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS
    )


async def _space_for_chat(chat_id: str) -> models.Space:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    if chat.space_id is None:
        raise fastapi.HTTPException(409, "chat has no space")
    space = await spaces.get(chat.space_id)
    if space is None:
        raise RuntimeError(f"chat {chat_id} belongs to unknown space {chat.space_id}")
    return space


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


@app.get("/api/chats/{chat_id}/devboxes/suggestion")
async def suggest_chat_devbox(chat_id: str) -> sandbox.Launch:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    space = await spaces.get(chat.space_id) if chat.space_id else None
    if space is None:
        found = await spaces.list_all()
        space = found[0] if found else await spaces.default()
    return await sandbox.suggest(space)


@app.post("/api/chats/{chat_id}/devboxes", status_code=202)
async def create_chat_devbox(chat_id: str, request: sandbox.Launch) -> dict:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    record = await sandbox.prepare(chat_id, request)
    _spawn(sandbox.provision(record))
    return {key: record.get(key) for key in ("id", "title", "repos", "state", "created_at")}


@app.get("/api/chats/{chat_id}/devboxes")
async def chat_devboxes(chat_id: str) -> list[dict]:
    """Chat-owned devboxes with their subagents and terminals, oldest first."""
    launches = await subagents.list_for_chat(chat_id)
    manual_terminals = await terminals.list_for_chat(chat_id)
    return [
        {
            **{
                key: record.get(key)
                for key in (
                    "id",
                    "title",
                    "repos",
                    "setup_script",
                    "ports",
                    "branch",
                    "git_sha",
                    "state",
                    "error",
                    "created_at",
                )
            },
            "subagents": [
                {
                    key: launch.get(key)
                    for key in (
                        "id",
                        "devbox_id",
                        "title",
                        "task_id",
                        "session_id",
                        "state",
                        "created_at",
                    )
                }
                for launch in launches
                if launch.get("devbox_id") == record["id"]
            ],
            "terminals": [
                {
                    key: terminal.get(key)
                    for key in (
                        "id",
                        "devbox_id",
                        "title",
                        "session_id",
                        "state",
                        "created_at",
                    )
                }
                for terminal in manual_terminals
                if terminal.get("devbox_id") == record["id"]
            ],
        }
        for record in await devboxes.list_for_chat(chat_id)
    ]


@app.post("/api/chats/{chat_id}/devboxes/{devbox_id}/terminals", status_code=201)
async def create_manual_terminal(chat_id: str, devbox_id: str) -> dict:
    workspace = await devboxes.get(devbox_id)
    if workspace is None or workspace.get("chat_id") != chat_id:
        raise fastapi.HTTPException(404, "unknown devbox")
    if not workspace.get("box"):
        raise fastapi.HTTPException(409, "devbox is not ready")
    found = await terminals.list_for_chat(chat_id)
    number = sum(terminal.get("devbox_id") == devbox_id for terminal in found) + 1
    terminal = await terminals.create(chat_id, devbox_id, f"bash {number}")
    await events.append(chat_id, "ui", {"type": "devbox.changed"})
    return terminal


@app.delete("/api/chats/{chat_id}/terminals/{terminal_id}", status_code=204)
async def delete_manual_terminal(chat_id: str, terminal_id: str) -> None:
    terminal = await terminals.get(terminal_id)
    if terminal is None or terminal.get("chat_id") != chat_id:
        raise fastapi.HTTPException(404, "unknown terminal")
    workspace = await devboxes.get(str(terminal.get("devbox_id", "")))
    if workspace is None or workspace.get("chat_id") != chat_id:
        raise fastapi.HTTPException(404, "unknown devbox")
    if terminal.get("session_id") and workspace.get("box"):
        await devbox.send_tty_input(
            workspace["box"]["url"], terminal["session_id"], b"\x03", b"exit\r"
        )
    await terminals.delete(terminal_id)
    await events.append(chat_id, "ui", {"type": "devbox.changed"})


@app.delete("/api/chats/{chat_id}/subagents/{launch_id}", status_code=204)
async def delete_subagent(chat_id: str, launch_id: str) -> None:
    launch = await subagents.get(launch_id)
    if launch is None or launch.get("chat_id") != chat_id:
        raise fastapi.HTTPException(404, "unknown subagent")
    workspace = await devboxes.get(str(launch.get("devbox_id", "")))
    if workspace is None or workspace.get("chat_id") != chat_id:
        raise fastapi.HTTPException(404, "unknown devbox")
    if (
        launch.get("state") not in devbox.TERMINAL_STATES
        and launch.get("session_id")
        and workspace.get("box")
    ):
        await devbox.send_tty_input(workspace["box"]["url"], launch["session_id"], b"\x03")
    if launch.get("task_id"):
        await devbox.delete_task(launch["task_id"])
    await subagents.delete(launch_id)
    await events.append(chat_id, "ui", {"type": "devbox.changed"})


@app.delete("/api/chats/{chat_id}/devboxes/{devbox_id}", status_code=204)
async def delete_chat_devbox(chat_id: str, devbox_id: str) -> None:
    workspace = await devboxes.get(devbox_id)
    if workspace is None or workspace.get("chat_id") != chat_id:
        raise fastapi.HTTPException(404, "unknown devbox")
    if workspace.get("state") == "creating":
        raise fastapi.HTTPException(409, "devbox is still being created")
    if workspace.get("box"):
        await devbox.delete_box(workspace["box"]["id"])
    await terminals.delete_for_devbox(devbox_id)
    await subagents.delete_for_devbox(devbox_id)
    await devboxes.delete(devbox_id)
    await events.append(chat_id, "ui", {"type": "devbox.changed"})


async def _bridge_tty(
    ws: fastapi.WebSocket,
    workspace: dict,
    session_id: str | None,
    on_handshake=None,
) -> None:
    q = ws.query_params
    url = devbox.tty_url(
        workspace["box"]["url"],
        session_id,
        q.get("offset", "0"),
        q.get("cols", "80"),
        q.get("rows", "24"),
    )
    try:
        # no max_size: the box may replay many MB of scrollback in one frame.
        async with websockets.connect(url, max_size=None) as box:

            async def down():
                async for frame in box:
                    text = frame if isinstance(frame, str) else frame.decode()
                    if on_handshake:
                        payload = json.loads(text)
                        if payload.get("type") == "handshake":
                            await on_handshake(payload["body"]["sessionId"])
                    await ws.send_text(text)

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
        await ws.close(code=4404, reason="terminal session not on the devbox yet")
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.websocket("/api/chats/{chat_id}/subagents/{launch_id}/tty")
async def task_tty(ws: fastapi.WebSocket, chat_id: str, launch_id: str) -> None:
    """Bridge the browser to one subagent's durable devbox PTY session."""
    launch = await subagents.get(launch_id)
    await ws.accept()
    if launch is None or launch.get("chat_id") != chat_id:
        await ws.close(code=4404, reason="unknown subagent")
        return
    workspace = await devboxes.get(str(launch.get("devbox_id", "")))
    if workspace is None or workspace.get("chat_id") != chat_id or not workspace.get("box"):
        await ws.close(code=4404, reason="subagent session not on the devbox yet")
        return
    if not launch.get("session_id") and launch.get("task_id"):
        task = await devbox.get_task(launch["task_id"])
        if task.get("session_id"):
            launch["session_id"] = task["session_id"]
            await subagents.save(launch)
    if not launch.get("session_id"):
        await ws.close(code=4404, reason="subagent session not on the devbox yet")
        return
    await _bridge_tty(ws, workspace, launch["session_id"])


@app.websocket("/api/chats/{chat_id}/terminals/{terminal_id}/tty")
async def manual_tty(ws: fastapi.WebSocket, chat_id: str, terminal_id: str) -> None:
    """Bridge the browser to a manual durable bash session."""
    terminal = await terminals.get(terminal_id)
    await ws.accept()
    if terminal is None or terminal.get("chat_id") != chat_id:
        await ws.close(code=4404, reason="unknown terminal")
        return
    workspace = await devboxes.get(str(terminal.get("devbox_id", "")))
    if workspace is None or workspace.get("chat_id") != chat_id or not workspace.get("box"):
        await ws.close(code=4404, reason="terminal session not on the devbox yet")
        return

    async def remember(session_id: str) -> None:
        if terminal.get("session_id") == session_id:
            return
        terminal["session_id"] = session_id
        terminal["state"] = "running"
        await terminals.save(terminal)

    await _bridge_tty(ws, workspace, terminal.get("session_id"), remember)


@app.post("/channels/v1/devbox")
async def devbox_webhook(body: dict, launch_id: str = "", secret: str = "") -> dict:
    """Persist one task event without disturbing sibling subagents."""
    kind = str(body.get("kind", ""))
    payload = body.get(kind)
    if kind not in ("taskStateChange", "assistantEvent") or not isinstance(payload, dict):
        raise fastapi.HTTPException(400, "unsupported devbox event")
    task_id = str(payload.get("taskId", ""))
    if not task_id:
        raise fastapi.HTTPException(400, "missing task id")

    record = await subagents.get(launch_id) if launch_id else None
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
    result = payload.get("result") if isinstance(payload.get("result"), dict) else None
    record, changed, previous = await subagents.apply_state(
        record["id"], state, result, seq=seq, remote_task_id=task_id
    )
    if not changed:
        if state in devbox.ACTIONABLE_STATES and not record.get("completion_delivered"):
            _spawn(complete_task(record["id"]))
            return {"ok": True}
        return {"ok": True, "duplicate": True}
    await activity.append(
        record["id"],
        "state_transition",
        {"from": previous, "to": state, "result": result or {}, "seq": seq},
    )
    if state not in devbox.ACTIONABLE_STATES:
        return {"ok": True}

    if not record.get("completion_delivered"):
        _spawn(complete_task(record["id"]))
    return {"ok": True}


async def complete_task(launch_id: str) -> None:
    """Run one serialized dispatcher turn for an actionable task state."""
    current = await subagents.get(launch_id)
    if current is None or current.get("state") not in devbox.ACTIONABLE_STATES:
        return
    record = await subagents.claim_completion(launch_id)
    if record is None:
        return

    generation = int(record["completion_generation"])
    chat_id = record["chat_id"]
    cursor = int(record.get("completion_cursor", -1))
    try:
        cached = str(record.get("completion_message") or "")
        if cached:
            outcome = CompletionOutcome(notify=True, message=cached)
        else:
            state = str(record.get("state", "unknown"))
            wake = ai.user_message(
                f"Handle subagent {launch_id} state {state!r}. Call check_subagent with "
                f"subagent_id={launch_id!r} and after={cursor}. Do not launch another subagent. "
                "If it needs attention, answer it with message_subagent when the answer is available "
                "from the conversation or workspace context; return notify=false. Otherwise ask the "
                "user for the missing input with notify=true. For completion or failure, return "
                "notify=true and a concise final message."
            )
            outcome = await _run_completion_turn(chat_id, {"id": chat_id}, wake)
            if outcome.message:
                record["completion_message"] = outcome.message
                await subagents.save(record)
        latest = await subagents.get(launch_id) or record
        if int(latest.get("completion_generation") or 0) != generation:
            return
        updates: dict = {"completion_cursor": await activity.cursor(launch_id)}
        if outcome.notify and outcome.message:
            failures = await _deliver(chat_id, outcome.message)
            updates["delivery_errors"] = failures
            if not failures:
                updates["completion_delivered"] = True
                updates["completion_message"] = outcome.message
        if latest.get("state") in devbox.TERMINAL_STATES:
            result = latest.get("result") or {}
            artifact = next(
                (
                    str(pr["url"])
                    for pr in result.get("prs") or []
                    if isinstance(pr, dict) and pr.get("url")
                ),
                outcome.message or "subagent completed",
            )
            siblings = await subagents.list_for_chat(chat_id)
            active = any(
                sibling["id"] != launch_id
                and sibling.get("state") not in devbox.TERMINAL_STATES
                for sibling in siblings
            )
            if not active:
                await chats.finish(
                    chat_id, "done" if latest.get("state") == "complete" else "failed", artifact
                )
        await events.append(
            chat_id,
            "ui",
            {
                "type": "task.changed",
                "launch_id": launch_id,
                "state": latest.get("state"),
            },
        )
        if outcome.notify and outcome.message:
            await events.append(chat_id, "ui", {"type": "messages.changed"})
        await subagents.finish_completion(launch_id, generation, **updates)
    except Exception:
        await subagents.finish_completion(launch_id, generation)
        raise


async def _emit(chat_id: str, event: channels.Event) -> list[str]:
    failures = []
    for binding in await chats.bindings(chat_id):
        channel = bot.channels.get(binding.channel)
        if channel is None:
            continue
        try:
            await channel.on_event(event, binding.state)
        except Exception as error:
            log.exception("channel delivery failed: %s -> %s", chat_id, binding.channel)
            failures.append(f"{binding.channel}: {error}")
    return failures


async def _deliver(chat_id: str, message: str) -> list[str]:
    return await _emit(
        chat_id, channels.event(channels.protocol.MESSAGE_COMPLETED, message=message)
    )


async def _run_inbound_turn(chat_id: str) -> None:
    async with turns.run(chat_id):
        await _emit(chat_id, channels.event(channels.protocol.TURN_STARTED))
        try:
            message = await _run_dispatcher_turn(chat_id, {"id": chat_id})
            await _deliver(chat_id, message)
        except Exception as error:
            log.exception("inbound dispatcher turn failed: %s", chat_id)
            await _emit(
                chat_id,
                channels.event(channels.protocol.TURN_FAILED, error=str(error)),
            )


async def _run_completion_turn(
    chat_id: str, record: dict, wake: ai.messages.Message
) -> CompletionOutcome:
    stored = await _transcript(chat_id)
    space = await _space_for_chat(chat_id)
    history = [ai.system_message(dispatcher.system_prompt(space)), *stored, wake]
    agent = dispatcher.agent_for(record)
    async with agent.run(dispatcher.model(), history, output_type=CompletionOutcome) as result:
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
    space = await _space_for_chat(chat_id)
    history = [ai.system_message(dispatcher.system_prompt(space)), *stored]
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
        "subagent completion recorded",
    )


# Keep the generic channel route after the concrete devbox callback. Starlette
# matches in registration order, so mounting it earlier would consume
# /channels/v1/devbox as an unknown channel before this module's route ran.
app.include_router(bot.router)
