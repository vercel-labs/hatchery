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
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets

import fastapi
import fastapi.middleware.cors
import fastapi.responses
import pydantic
import websockets.asyncio.client

import ai
import ai.ui.ai_sdk.outbound_stream
import ai.ui.ai_sdk.ui_events
import auth
import channels
import models
import store
import vercel.functions
import vercel.queue
from agent import classifier, dispatcher, sandbox, telemetry, topic
import worker
from worker import protocol as worker_protocol
from worker import signing
from channels import github, slack
from store import chats, events, spaces, turns

log = logging.getLogger("app")
_background: set[asyncio.Task] = set()

# This module is also the queue subscriber entrypoint, where FastAPI's lifespan
# does not run.
telemetry.install()


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
        async with ai.experimental_telemetry.span("channel.dispatch") as span:
            span.set_attrs(channel=channel)
            found = await spaces.list_all() or [await spaces.default()]
            title = inbound.title or inbound.text.strip().splitlines()[0][:80]
            chat, created = await chats.claim(
                f"{channel}:{inbound.token}",
                channel,
                None,
                title,
                inbound.state,
            )
            span.set_attrs(
                {"chat.id": chat.id, "space.id": chat.space_id or ""},
                created=created,
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
            span.set_attrs({"space.id": chat.space_id or ""})
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
    async with ai.experimental_telemetry.span("hatchery.classify") as span:
        span.set_attrs(
            {"chat.id": chat_id},
            origin=str(metadata.get("origin", "unknown")),
            candidate_count=len(candidates),
        )
        await _emit(chat_id, channels.event(channels.protocol.SPACE_ASSIGNING))
        selected = await classifier.classify(prompt, metadata, candidates)
        span.set_attrs({"space.id": selected.id})
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
    telemetry.install()
    await store.ensure_ready()
    await spaces.default()
    yield
    telemetry.flush()


app = fastapi.FastAPI(title="hatchery", lifespan=lifespan)


@app.middleware("http")
async def browser_session(request: fastapi.Request, call_next):
    path = request.url.path
    public = (
        path == "/api/health"
        or path.startswith("/api/auth/")
        or path.startswith("/channels/")
    )
    user = await auth.current_user(request) if path.startswith("/api/") and not public else None
    if path.startswith("/api/") and not public and user is None:
        return fastapi.responses.JSONResponse({"detail": "sign in required"}, status_code=401)
    match = re.match(r"^/api/chats/([^/]+)", path)
    if match is not None and user is not None:
        chat = await chats.claim_user(match.group(1), user["id"])
        if chat is not None and chat.user_id != user["id"]:
            return fastapi.responses.JSONResponse({"detail": "unknown chat"}, status_code=404)
    if path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"} and not auth.valid_origin(request):
        return fastapi.responses.JSONResponse({"detail": "invalid origin"}, status_code=403)
    return await call_next(request)


# local dev: the ui talks to :8000 directly for streams — next's dev proxy
# severs quiet/long sse responses (and can't proxy websockets at all).
app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "channels": list(bot.channels)}


@app.get("/api/auth/login")
async def auth_login(request: fastapi.Request):
    return await auth.begin(request)


@app.get("/api/auth/callback")
async def auth_callback(request: fastapi.Request, code: str = "", state: str = ""):
    if not code or not state:
        raise fastapi.HTTPException(400, "missing OAuth code or state")
    return await auth.callback(request, code, state)


@app.get("/api/auth/me")
async def auth_me(request: fastapi.Request) -> dict:
    return {"user": await auth.current_user(request)}


@app.post("/api/auth/logout")
async def auth_logout(request: fastapi.Request):
    return await auth.logout(request)


@app.get("/api/connections/github")
async def github_connection(request: fastapi.Request) -> dict:
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    connection = auth.github_connection(user)
    if connection is None:
        return {"connection": None}
    try:
        await auth.github_token(user["id"], connection.get("installation_id"))
    except (
        auth.connect.UserAuthorizationRequiredError,
        auth.connect.NoValidTokenError,
        auth.connect.ConnectorInstallationRequiredError,
    ):
        return {"connection": None}
    return {"connection": connection}


@app.get("/api/connections/github/authorize")
async def authorize_github(request: fastapi.Request):
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    return await auth.begin_github(request, user)


@app.get("/api/connections/github/return")
async def github_return(request: fastapi.Request):
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    try:
        return await auth.finish_github(user)
    except (
        auth.connect.UserAuthorizationRequiredError,
        auth.connect.NoValidTokenError,
        auth.connect.ConnectorInstallationRequiredError,
    ) as error:
        raise fastapi.HTTPException(409, "GitHub authorization was not completed") from error


@app.delete("/api/connections/github", status_code=204)
async def disconnect_github(request: fastapi.Request) -> None:
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    await auth.disconnect_github(user)


class VercelCLIRequest(pydantic.BaseModel):
    token: str = pydantic.Field(min_length=1, max_length=512)


@app.get("/api/connections/vercel-cli")
async def vercel_cli_connection(request: fastapi.Request) -> dict:
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    return {"connection": await auth.vercel_cli_connection(user["id"])}


@app.put("/api/connections/vercel-cli")
async def connect_vercel_cli(
    request: fastapi.Request, body: VercelCLIRequest
) -> dict:
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    try:
        connection = await auth.connect_vercel_cli(user["id"], body.token)
    except ValueError as error:
        raise fastapi.HTTPException(400, str(error)) from error
    return {"connection": connection}


@app.delete("/api/connections/vercel-cli", status_code=204)
async def disconnect_vercel_cli(request: fastapi.Request) -> None:
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    await auth.disconnect_vercel_cli(user["id"])


class SpaceWarning(pydantic.BaseModel):
    space_id: str
    repo: str
    warning: str


@app.get("/api/spaces")
async def list_spaces() -> list[models.Space]:
    found = await spaces.list_all()
    return found or [await spaces.default()]


@app.get("/api/spaces/warnings")
async def space_warnings(request: fastapi.Request) -> list[SpaceWarning]:
    user = await auth.current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "sign in required")
    warnings = []
    for space in await spaces.list_all() or [await spaces.default()]:
        if not space.repos:
            continue
        repo = space.repos[0]
        try:
            warning = await auth.github_repo_warning(user["id"], repo)
        except (
            auth.connect.UserAuthorizationRequiredError,
            auth.connect.NoValidTokenError,
            auth.connect.ConnectorInstallationRequiredError,
        ):
            warning = f"Connect GitHub to let Hatchery make pull requests to {repo}."
        if warning is None:
            continue
        log.warning(
            "space main repository lacks Hatchery GitHub access",
            extra={"space_id": space.id, "repo": repo, "user_id": user["id"]},
        )
        warnings.append(SpaceWarning(space_id=space.id, repo=repo, warning=warning))
    return warnings


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
async def list_chats(request: fastapi.Request) -> list[models.Chat]:
    user = await auth.current_user(request)
    found = []
    if user is not None:
        for chat in await chats.list_all():
            if chat.user_id is None:
                chat = await chats.claim_user(chat.id, user["id"]) or chat
            if chat.user_id == user["id"]:
                found.append(chat)
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
async def create_chat(request: CreateChatRequest, http_request: fastapi.Request) -> models.Chat:
    user = await auth.current_user(http_request)
    found = await spaces.list_all()
    if not found:
        found = [await spaces.default()]
    if request.space_id is not None and not any(space.id == request.space_id for space in found):
        raise fastapi.HTTPException(404, "unknown space")
    return await chats.create(
        request.space_id,
        request.title,
        user_id=user["id"] if user is not None else None,
    )


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
    """The stored transcript as UI messages, with internal messages hidden."""
    transcript = [
        message
        for message in await _transcript(chat_id)
        if (message.provider_metadata or {}).get("hatchery", {}).get("kind")
        != "subagent_result"
    ]
    messages = ai.ui.ai_sdk.to_ui_messages(transcript)
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
        async with turns.run(request.chat_id), ai.experimental_telemetry.span(
            "hatchery.turn"
        ) as span:
            span.set_attrs({"chat.id": request.chat_id}, origin="ui")
            stored = await _transcript(request.chat_id)
            span.set_attrs(stored_message_count=len(stored))
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
            span.set_attrs(received_message_count=len(received))
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
                span.set_attrs(classification_required=True)
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
            span.set_attrs({"space.id": space.id})
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
            finally:
                telemetry.flush()

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
    try:
        created = await sandbox.create(chat_id, request)
    except RuntimeError as error:
        raise fastapi.HTTPException(409, str(error)) from error
    return created.model_dump(exclude={"daemon_token"})


@app.get("/api/chats/{chat_id}/sandboxes")
async def chat_sandboxes(chat_id: str) -> list[dict]:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    tasks = await worker.store.list_tasks(chat_id)
    terminals = await worker.list_terminals(chat_id)
    result = []
    for item in await sandbox.list_all(chat_id):
        data = item.model_dump(exclude={"daemon_token"})
        data["subagents"] = [
            {
                **task.model_dump(),
                "sandbox_id": task.worker_id,
                "task_id": task.id,
                "session_id": task.id,
            }
            for task in tasks
            if task.worker_id == item.id
        ]
        data["terminals"] = [
            {
                **terminal.model_dump(),
                "sandbox_id": terminal.worker_id,
                "session_id": terminal.id,
            }
            for terminal in terminals
            if terminal.worker_id == item.id
        ]
        result.append(data)
    return result


@app.post("/api/chats/{chat_id}/sandboxes/{sandbox_id}/terminals", status_code=201)
async def create_manual_terminal(chat_id: str, sandbox_id: str) -> dict:
    try:
        terminal = await worker.create_terminal(chat_id, sandbox_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise fastapi.HTTPException(409, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})
    return {**terminal.model_dump(), "sandbox_id": terminal.worker_id, "session_id": terminal.id}


@app.delete("/api/chats/{chat_id}/terminals/{terminal_id}", status_code=204)
async def delete_manual_terminal(chat_id: str, terminal_id: str) -> None:
    try:
        await worker.delete_terminal(chat_id, terminal_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


@app.delete("/api/chats/{chat_id}/subagents/{subagent_id}", status_code=204)
async def delete_subagent(chat_id: str, subagent_id: str) -> None:
    try:
        await worker.delete_task(chat_id, subagent_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


@app.delete("/api/chats/{chat_id}/sandboxes/{sandbox_id}", status_code=204)
async def delete_chat_sandbox(chat_id: str, sandbox_id: str) -> None:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    try:
        await sandbox.destroy(chat_id, sandbox_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error


async def _authenticate_websocket(ws: fastapi.WebSocket) -> bool:
    if not auth.valid_origin(ws):
        await ws.accept()
        await ws.close(code=4403, reason="invalid origin")
        return False
    session_id = getattr(ws, "cookies", {}).get(auth.COOKIE, "")
    if await auth.session_user(session_id) is not None:
        return True
    await ws.accept()
    await ws.close(code=4401, reason="sign in required")
    return False


async def _bridge_tty(
    ws: fastapi.WebSocket,
    record: worker.Worker,
    session_id: str,
    command: list[str] | None = None,
) -> None:
    url, headers = worker.sandbox.tty(record)
    attach = {
        "session_id": session_id,
        "offset": int(ws.query_params.get("offset", "0")),
        "cols": int(ws.query_params.get("cols", "80")),
        "rows": int(ws.query_params.get("rows", "24")),
    }
    if command is not None:
        attach["command"] = command
    await ws.accept()
    close_code = 1000
    close_reason = ""
    try:
        async with websockets.asyncio.client.connect(
            url, additional_headers=headers, max_size=None, compression=None
        ) as upstream:
            await upstream.send(json.dumps(attach))

            async def down() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await ws.send_bytes(message)
                    else:
                        await ws.send_text(message)

            async def up() -> None:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            done, pending = await asyncio.wait(
                [asyncio.create_task(down()), asyncio.create_task(up())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except fastapi.WebSocketDisconnect:
        pass
    except websockets.ConnectionClosed as error:
        if error.rcvd is not None:
            close_code = error.rcvd.code
            close_reason = error.rcvd.reason
        else:
            close_code = 1011
            close_reason = "upstream connection closed"
    except websockets.InvalidStatus as error:
        status = error.response.status_code
        close_code = 4401 if status == 401 else 4403 if status == 403 else 1011
        close_reason = f"upstream rejected connection ({status})"
    except Exception as error:
        log.warning("TTY bridge failed for sandbox %s session %s: %s", record.id, session_id, error)
        close_code = 1011
        close_reason = "upstream connection failed"
    finally:
        with contextlib.suppress(RuntimeError):
            await ws.close(code=close_code, reason=close_reason)


@app.websocket("/api/chats/{chat_id}/sandboxes/{sandbox_id}/ssh")
async def sandbox_ssh(ws: fastapi.WebSocket, chat_id: str, sandbox_id: str) -> None:
    if not await _authenticate_websocket(ws):
        return
    record = await worker.get(sandbox_id)
    if record is None or record.chat_id != chat_id:
        await ws.accept()
        await ws.close(code=4404, reason="unknown sandbox")
        return
    url, headers = worker.sandbox.ssh(record)
    query = str(ws.url.query)
    if query:
        url += "?" + query
    await ws.accept()
    try:
        async with websockets.asyncio.client.connect(
            url, additional_headers=headers, max_size=None, compression=None
        ) as upstream:
            async def down() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await ws.send_bytes(message)
                    else:
                        await ws.send_text(message)

            async def up() -> None:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            done, pending = await asyncio.wait(
                [asyncio.create_task(down()), asyncio.create_task(up())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except (fastapi.WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    finally:
        with contextlib.suppress(RuntimeError):
            await ws.close()


@app.get("/api/chats/{chat_id}/subagents/{subagent_id}/readiness")
async def task_readiness(chat_id: str, subagent_id: str) -> dict:
    task = await worker.get_task(chat_id, subagent_id)
    if task is None:
        raise fastapi.HTTPException(404, "unknown subagent")
    record = await worker.get(task.worker_id)
    if record is None:
        raise fastapi.HTTPException(404, "unknown sandbox")
    try:
        daemon, sessions = await asyncio.gather(
            worker.sandbox.daemon_health(record),
            worker.sandbox.tty_sessions(record),
        )
    except Exception as error:
        log.warning("daemon readiness failed for sandbox %s: %s", record.id, error)
        daemon = {
            "ok": False,
            "queue_connected": False,
            "queue_error": "sandbox daemon is unreachable",
        }
        sessions = []
    return {
        "state": task.status,
        "session_ready": any(session.get("id") == task.id for session in sessions),
        "daemon": daemon,
    }


@app.websocket("/api/chats/{chat_id}/subagents/{subagent_id}/tty")
async def task_tty(ws: fastapi.WebSocket, chat_id: str, subagent_id: str) -> None:
    if not await _authenticate_websocket(ws):
        return
    task = await worker.get_task(chat_id, subagent_id)
    if task is None:
        await ws.accept()
        await ws.close(code=4404, reason="unknown subagent")
        return
    if task.status == "pending":
        await ws.accept()
        await ws.close(code=4409, reason="subagent is waiting for the sandbox queue")
        return
    record = await worker.get(task.worker_id)
    if record is None:
        await ws.accept()
        await ws.close(code=4404, reason="unknown sandbox")
        return
    await _bridge_tty(ws, record, task.id)


@app.websocket("/api/chats/{chat_id}/terminals/{terminal_id}/tty")
async def manual_tty(ws: fastapi.WebSocket, chat_id: str, terminal_id: str) -> None:
    if not await _authenticate_websocket(ws):
        return
    terminal = await worker.store.get_terminal(terminal_id)
    if terminal is None or terminal.chat_id != chat_id:
        await ws.accept()
        await ws.close(code=4404, reason="unknown terminal")
        return
    record = await worker.get(terminal.worker_id)
    if record is None:
        await ws.accept()
        await ws.close(code=4404, reason="unknown sandbox")
        return
    await _bridge_tty(ws, record, terminal.id, ["/bin/bash", "-l"])


async def complete_signing(event: worker_protocol.Event) -> None:
    request_id = str(event.payload.get("request_id") or "")
    body = event.payload.get("request") or {}
    record = await worker.get(event.worker_id)
    error = ""
    signed: list[str] = []
    supplied = str(event.payload.get("signature") or "")
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected = (
        hmac.new(
            record.daemon_token.encode(),
            f"{request_id}:{serialized}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if record is not None
        else ""
    )
    if not request_id or not supplied or not secrets.compare_digest(supplied, expected):
        error = "invalid commit signing request"
    else:
        repo = body.get("repo") or {}
        requested = f"{repo.get('owner', '')}/{repo.get('name', '')}"
        if requested not in record.spec.repos:
            error = "repository is not attached to this worker"
        elif not record.user_id:
            error = "commit signing requires a connected GitHub user"
        else:
            try:
                signed = await signing.sign_commits(
                    os.environ["GITHUB_CONNECTOR"], body
                )
            except Exception as caught:
                error = str(caught)
    await vercel.queue.send(
        worker_protocol.command_topic(event.worker_id),
        worker_protocol.command(
            event.worker_id,
            0,
            "sign.failed" if error else "sign.completed",
            payload={
                "request_id": request_id,
                "error": error,
                "signed_shas": signed,
            },
            command_id=f"cmd_{event.id}",
        ).model_dump(mode="json"),
        idempotency_key=f"cmd_{event.id}",
        deployment=vercel.queue.ALL_DEPLOYMENTS,
    )


@vercel.queue.subscribe(
    topic=worker_protocol.EVENT_TOPIC,
    consumer_group="hatchery-control-plane-v1",
    max_concurrency=1,
)
async def worker_event(event: worker_protocol.Event) -> None:
    """Persist one at-least-once worker event and wake the owning chat."""
    if event.type == "sign.requested":
        await complete_signing(event)
        return
    task = await worker.store.get_task(event.task_id) if event.task_id else None
    parent = (
        ai.experimental_telemetry.Span[
            ai.experimental_telemetry.CustomSpanData
        ].model_validate(task.telemetry_span)
        if task is not None and task.telemetry_span
        else None
    )
    terminal = False
    span_name = (
        f"fx.{event.payload.get('kind') or 'event'}"
        if event.type == "task.transcript"
        else "fx.assistant"
        if event.type == "task.output"
        else "fx.attention"
        if event.type == "task.question"
        else f"fx.{event.type}"
    )
    try:
        async with ai.experimental_telemetry.use_span(parent):
            async with ai.experimental_telemetry.span(span_name) as span:
                span.set_attrs(
                    {
                        "worker.id": event.worker_id,
                        "task.id": event.task_id or "",
                        "event.id": event.id,
                    },
                    event_type=event.type,
                    sequence=event.sequence,
                )
                kind = str(event.payload.get("kind") or "event")
                if event.type == "task.transcript" and kind == "tool.call":
                    arguments = str(event.payload.get("arguments") or "{}")
                    try:
                        tool_input = json.loads(arguments)
                    except json.JSONDecodeError:
                        tool_input = arguments
                    span.set_attrs(
                        {
                            "braintrust.input_json": json.dumps(tool_input),
                            "braintrust.span_attributes": json.dumps({"type": "tool"}),
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": str(event.payload.get("tool_name") or "fx"),
                            "gen_ai.tool.type": "function",
                            "gen_ai.tool.call.id": str(event.payload.get("tool_call_id") or ""),
                            "gen_ai.tool.call.arguments": arguments,
                        }
                    )
                elif event.type == "task.transcript" and kind == "tool.result":
                    output = str(event.payload.get("output") or "")
                    span.set_attrs(
                        {
                            "braintrust.output_json": json.dumps(output),
                            "braintrust.span_attributes": json.dumps({"type": "tool"}),
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.call.id": str(event.payload.get("tool_call_id") or ""),
                            "gen_ai.tool.call.result": json.dumps(output),
                        },
                        tool_error=bool(event.payload.get("error")),
                    )
                elif event.type == "task.transcript" and kind == "user":
                    text = str(event.payload.get("text") or "")
                    span.set_attrs(
                        {"braintrust.input_json": json.dumps({"text": text})}
                    )
                elif event.type == "task.output":
                    text = str(event.payload.get("text") or "")
                    span.set_attrs(
                        {"braintrust.output_json": json.dumps({"text": text[:8192]})}
                    )
                task, changed = await worker.ingest(event)
                span.set_attrs(applied=changed)
                if changed and parent is not None and event.type == "task.transcript":
                    kind = str(event.payload.get("kind") or "event")
                    parent.add_event(f"fx.{kind}", event.payload)
                elif changed and parent is not None and event.type == "task.output":
                    text = str(event.payload.get("text") or "")
                    parent.add_event(
                        "fx.assistant",
                        {
                            "text": text[:8192],
                            "truncated": len(text) > 8192,
                            "session_id": event.payload.get("session_id"),
                        },
                    )
                elif changed and parent is not None and event.type == "task.question":
                    question = str(event.payload.get("question") or event.payload.get("text") or "")
                    parent.add_event("fx.attention", {"text": question[:8192]})
                elif changed and parent is not None and event.type == "task.completed":
                    parent.add_event("fx.turn.completed")
                if task is not None:
                    span.set_attrs({"chat.id": task.chat_id}, task_state=task.status)
                    if changed and parent is not None:
                        parent.set_attrs(
                            {
                                "fx.session_id": task.fx_session_id or "",
                                "fx.transcript_event_count": task.transcript_event_count,
                                "fx.tool_call_count": task.transcript_tool_call_count,
                                "fx.truncated_event_count": task.transcript_truncated_count,
                            }
                        )

                        def save_run(current: worker.Task) -> worker.Task:
                            current.telemetry_span = parent.model_dump(mode="json")
                            return current

                        await worker.store.mutate_task(task.id, save_run)
                terminal = bool(
                    changed
                    and task is not None
                    and event.type in ("task.completed", "task.failed")
                )
                if task is not None and event.type in (
                    "task.question",
                    "task.completed",
                    "task.failed",
                ):
                    await complete_worker_task(task)
        if terminal and task is not None and parent is not None:
            parent.set_attrs(
                {"braintrust.output_json": json.dumps(task.result)},
                task_state=task.status,
            )
            parent.stamp_end()

            def close_run(current: worker.Task) -> worker.Task:
                current.telemetry_span = parent.model_dump(mode="json")
                return current

            await worker.store.mutate_task(task.id, close_run)
            await parent.push()
        elif changed and parent is not None and parent.ended_at is not None:
            await parent.push()
    finally:
        telemetry.flush()


async def complete_worker_task(task: worker.Task) -> None:
    """Persist one internal result, then let the dispatcher continue the chat."""
    async with turns.run(task.chat_id):
        current = await worker.get_task(task.chat_id, task.id)
        if current is None or current.completion_delivered:
            return
        if current.completion_sequence != current.event_sequence:
            result_message = ai.user_message(
                "<subagent_result>\n"
                + json.dumps(
                    {
                        "subagent_id": current.id,
                        "status": current.status,
                        "result": current.result or {},
                    },
                    separators=(",", ":"),
                )
                + "\n</subagent_result>"
            )
            result_message.id = f"subagent_result_{current.id}_{current.event_sequence}"
            result_message.provider_metadata = {
                "hatchery": {"kind": "subagent_result", "subagent_id": current.id}
            }
            transcript = await _transcript(current.chat_id)
            if all(message.id != result_message.id for message in transcript):
                await events.append(
                    current.chat_id,
                    "messages",
                    result_message.model_dump(mode="json"),
                )
            current.completion_sequence = current.event_sequence
            current.completion_message = None
            await worker.store.save_task(current)

        message = current.completion_message
        if not message:
            transcript = await _transcript(current.chat_id)
            result_index = next(
                (
                    index
                    for index, item in enumerate(transcript)
                    if item.id
                    == f"subagent_result_{current.id}_{current.completion_sequence}"
                ),
                -1,
            )
            message = next(
                (
                    item.text
                    for item in reversed(transcript[result_index + 1 :])
                    if item.role == "assistant" and item.text
                ),
                "",
            )
        if not message:
            message = await _run_dispatcher_turn(current.chat_id, {"id": current.chat_id})
        if current.completion_message != message:
            current = await worker.get_task(current.chat_id, current.id) or current
            current.completion_message = message
            await worker.store.save_task(current)

        failures = await _deliver(current.chat_id, message)
        if failures:
            return
        current.completion_delivered = True
        await worker.store.save_task(current)
        if current.status in ("complete", "errored"):
            siblings = await worker.store.list_tasks(current.chat_id)
            if not any(
                sibling.id != current.id
                and sibling.status in ("pending", "running", "attention")
                for sibling in siblings
            ):
                await chats.finish(
                    current.chat_id,
                    "failed" if current.status == "errored" else "done",
                    message,
                )
        await events.append(current.chat_id, "ui", {"type": "messages.changed"})
        await events.append(current.chat_id, "ui", {"type": "chat.changed"})


async def _emit(chat_id: str, event: channels.Event) -> list[str]:
    async with ai.experimental_telemetry.span("channel.deliver") as span:
        bindings = await chats.bindings(chat_id)
        span.set_attrs(
            {"chat.id": chat_id},
            event_type=event.type,
            binding_count=len(bindings),
        )
        failures = []
        for binding in bindings:
            channel = bot.channels.get(binding.channel)
            if channel is None:
                continue
            try:
                await channel.on_event(event, binding.state)
            except Exception as error:
                log.exception("channel delivery failed: %s -> %s", chat_id, binding.channel)
                failures.append(f"{binding.channel}: {error}")
        span.set_attrs(failure_count=len(failures))
        return failures


async def _deliver(chat_id: str, message: str) -> list[str]:
    return await _emit(
        chat_id, channels.event(channels.protocol.MESSAGE_COMPLETED, message=message)
    )


async def _run_inbound_turn(chat_id: str) -> None:
    async with turns.run(chat_id), ai.experimental_telemetry.span(
        "hatchery.turn"
    ) as span:
        span.set_attrs({"chat.id": chat_id}, origin="channel")
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
    try:
        async with agent.run(dispatcher.model(), history) as result:
            async for _ in result:
                pass
            added = result.messages[len(history) :]
            for message in added:
                await events.append(chat_id, "messages", message.model_dump(mode="json"))
    finally:
        telemetry.flush()
    return next(
        (message.text for message in reversed(added) if message.role == "assistant" and message.text),
        "subagent completion recorded",
    )


app.include_router(bot.router)
