"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/hatchery")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)

State lives in the store (postgres via DATABASE_URL, local files without):
a chat's transcript is its (chat_id, "messages") stream.
Slack/github inbound lands in its chat via
_StoreHub (dedupe, claim binding, append); no turn runs on inbound yet.
"""

import asyncio
import contextlib
import html
import json
import logging
import os
import re

import fastapi
import fastapi.middleware.cors
import fastapi.responses
import pydantic

import ai
import ai.ui.ai_sdk.outbound_stream
import ai.ui.ai_sdk.ui_events
import channels
import models
import store
import vercel.functions
import vercel.queue
from agent import classifier, dispatcher, sandbox, topic
import worker
from worker import protocol as worker_protocol
from channels import github, slack
from store import chats, events, spaces, turns

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


@app.get("/api/chats/{chat_id}/sandboxes/suggestion")
async def suggest_chat_sandbox(chat_id: str) -> sandbox.Launch:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    space = await spaces.get(chat.space_id) if chat.space_id else None
    if space is None:
        found = await spaces.list_all()
        space = found[0] if found else await spaces.default()
    return await sandbox.suggest(space)


@app.post("/api/chats/{chat_id}/sandboxes")
async def create_chat_sandbox(chat_id: str, request: sandbox.Launch) -> dict:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    created = await sandbox.create(chat_id, request)
    return created.model_dump(exclude={"daemon_token"})


@app.get("/api/chats/{chat_id}/sandboxes")
async def chat_sandboxes(chat_id: str) -> list[dict]:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    return [
        item.model_dump(exclude={"daemon_token"})
        for item in await sandbox.list_all(chat_id)
    ]


@app.delete("/api/chats/{chat_id}/sandboxes/{sandbox_id}", status_code=204)
async def delete_chat_sandbox(chat_id: str, sandbox_id: str) -> None:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    try:
        await sandbox.destroy(chat_id, sandbox_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error


@app.websocket("/api/chats/{chat_id}/subagents/{subagent_id}/tty")
async def task_tty(ws: fastapi.WebSocket, chat_id: str, subagent_id: str) -> None:
    await ws.accept()
    await ws.close(code=1011, reason="sandbox TTY is not implemented")


@vercel.queue.subscribe(
    topic=worker_protocol.EVENT_TOPIC,
    consumer_group="hatchery-control-plane-v1",
)
async def worker_event(event: worker_protocol.Event) -> None:
    """Persist one at-least-once worker event and wake the owning chat."""
    task, changed = await worker.ingest(event)
    if changed and task is not None and task.status in ("attention", "complete", "errored"):
        await complete_worker_task(task)


async def complete_worker_task(task: worker.Task) -> None:
    """Record and deliver one actionable subagent result."""
    current = await worker.get_task(task.chat_id, task.id)
    if current is None or current.completion_delivered:
        return
    result = current.result or {}
    if current.status == "attention":
        message = str(result.get("question") or "subagent needs input")
    elif current.status == "errored":
        message = f"Subagent failed: {result.get('error') or 'unknown error'}"
    else:
        message = str(result.get("summary") or "subagent completed")
    await events.append(
        current.chat_id,
        "messages",
        ai.assistant_message(message).model_dump(mode="json"),
    )
    failures = await _deliver(current.chat_id, message)
    if not failures:
        current.completion_delivered = True
        await worker.store.save_task(current)
    await chats.finish(
        current.chat_id,
        "failed" if current.status == "errored" else "done",
        message,
    )
    await events.append(current.chat_id, "ui", {"type": "messages.changed"})
    await events.append(current.chat_id, "ui", {"type": "chat.changed"})


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


app.include_router(bot.router)
