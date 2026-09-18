"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the dispatcher chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/hatchery")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /api/chat            dispatcher agent turn, AI SDK UI message stream (SSE)

Application projections live in the store (Postgres via DATABASE_URL, local
files without). Rotor checkpoints the canonical dispatcher conversation; the
(chat_id, "messages") stream feeds the UI and bootstraps existing chats.
Slack/GitHub inbound lands in its chat through _StoreHub, then enters the
chat's Rotor mailbox.
"""

import asyncio
import contextlib
import dataclasses
import datetime
import hashlib
import hmac
import html
import json
import logging
import os
import pathlib
import re
import urllib.parse

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
import connections
import models
import store
import vercel.functions
import vercel.queue
from agent import (
    classifier,
    durable,
    runtime as rotor_runtime,
    sandbox,
    schedule_runtime,
    stream as agent_stream,
    telemetry,
    topic,
)
import worker
from worker import hierarchy as worker_hierarchy
from worker import protocol as worker_protocol
from channels import github, slack
from store import agent_files, agents, chats, events, jobs, settings, turns

log = logging.getLogger("app")
_background: set[asyncio.Task] = set()
_rotor_promotion_lock = asyncio.Lock()

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


async def _channel_user(channel: str, state: dict) -> tuple[str | None, dict | None]:
    user_id = None
    if channel == "slack":
        user_id = await connections.auth_store.slack_user(
            str(state.get("team_id", "")), str(state.get("user_id", ""))
        )
    elif channel == "github":
        user_id = await connections.auth_store.github_user(
            str(state.get("sender_id", ""))
        )
    user = await connections.auth_store.get_user(user_id) if user_id else None
    return user_id, user


class _StoreHub:
    """Land inbound messages in a chat and run one dispatcher turn.

    The channel endpoint already defers dispatch until after its fast ack, so
    this coroutine can own the full turn and its reply delivery.
    """

    async def dispatch(self, channel: str, inbound: channels.Inbound) -> None:
        async with ai.experimental_telemetry.span("channel.route") as span:
            span.set_attrs(channel=channel)
            actor_id, actor = await _channel_user(
                channel, inbound.actor or inbound.state
            )
            if inbound.actor is None or inbound.actor == inbound.state:
                author_user = actor
            else:
                _, author_user = await _channel_user(channel, inbound.state)
            found = await agents.list_all() or [await agents.default()]
            title = inbound.title or inbound.text.strip().splitlines()[0][:80]
            token = f"{channel}:{inbound.token}"
            legacy_token = None
            if channel == "slack":
                token = f"slack:{inbound.state['team_id']}:{inbound.token}"
                legacy_token = f"slack:{inbound.token}"
            linked = await chats.binding(token)
            actor_allowed = auth.allowed_user(actor)
            if channel in {"slack", "github"} and linked is None and not actor_allowed:
                span.set_attrs(ignored=f"unconnected_or_disallowed_{channel}_user")
                return
            if linked is not None:
                chat = await chats.get(linked.chat_id)
                if chat is None:
                    raise ValueError("binding refers to a missing chat")
                await chats.bind(token, chat.id, channel, inbound.state)
                created = False
            else:
                chat, created = await chats.claim(
                    token,
                    channel,
                    None,
                    title,
                    inbound.state,
                    user_id=actor_id,
                    author_display_name=_user_display_name(actor),
                    legacy_token=legacy_token,
                )
            span.set_attrs({"chat.id": chat.id}, accepted=True)
            async with telemetry.use_chat(chat.id):
                async with ai.experimental_telemetry.span(
                    "channel.dispatch"
                ) as dispatch_span:
                    async with turns.run(chat.id):
                        chat = await chats.get(chat.id) or chat
                        dispatch_span.set_attrs(
                            {"chat.id": chat.id, "agent.id": chat.agent_id or ""},
                            channel=channel,
                            created=created,
                        )
                        if chat.archived_at is not None:
                            dispatch_span.set_attrs(ignored="archived")
                            await _deliver(
                                chat.id,
                                "This chat is archived. Unarchive it in Hatchery before posting.",
                            )
                            return
                        source_id = str(inbound.state.get("message_id", ""))
                        message = ai.user_message(inbound.text)
                        if source_id:
                            message.id = (
                                "inbound_"
                                + hashlib.sha256(
                                    f"{token}:{source_id}".encode()
                                ).hexdigest()
                            )
                        author = _user_display_name(author_user) or _inbound_author(
                            inbound
                        )
                        display_text = str(
                            inbound.state.get("display_text", inbound.text)
                        )
                        message.provider_metadata = {
                            "hatchery": {
                                "origin": channel,
                                "author": author,
                                "display_text": display_text,
                                "actor_user_id": actor_id if actor_allowed else None,
                            }
                        }
                        known = {saved.id for saved in await _transcript(chat.id)}
                        stored = inbound.persist and message.id not in known
                        if stored:
                            await events.append(
                                chat.id, "messages", message.model_dump(mode="json")
                            )
                            await events.append(
                                chat.id, "ui", {"type": "messages.changed"}
                            )
                            await _emit(
                                chat.id,
                                channels.event(
                                    channels.protocol.MESSAGE_RECEIVED,
                                    message=display_text,
                                    message_id=message.id,
                                    origin=channel,
                                    author=author,
                                    source_binding=token,
                                ),
                            )
                        elif inbound.persist and message.id in known:
                            dispatch_span.set_attrs(
                                ignored="duplicate_message", **{"chat.id": chat.id}
                            )
                            return

                        if chat.agent_id is None and (
                            actor_allowed or channel not in {"slack", "github"}
                        ):
                            await _classify_chat(
                                chat.id,
                                inbound.text,
                                {
                                    "origin": channel,
                                    "author": author,
                                    "repo": inbound.repo,
                                    "channel_state": inbound.state,
                                },
                                found,
                            )
                            chat = await chats.get(chat.id) or chat
                        if created:
                            _spawn(_name_chat(chat.id, inbound.text))
                        invoke = inbound.invoke and (
                            actor_allowed or channel not in {"slack", "github"}
                        )
                        dispatch_span.set_attrs(
                            {"agent.id": chat.agent_id or ""}, invoke=invoke
                        )
                        log.info(
                            "inbound %s -> %s chat %s",
                            channel,
                            "new" if created else "existing",
                            chat.id,
                        )
                        if invoke:
                            await _run_inbound_turn(chat.id, actor_id)

    async def authorize(self, channel: str, inbound: channels.Inbound) -> bool:
        _, user = await _channel_user(channel, inbound.actor or inbound.state)
        return auth.allowed_user(user)

    async def dedupe(self, key: str) -> bool:
        return await chats.dedupe(key)

    async def binding(self, channel: str, token: str) -> dict | None:
        binding = await chats.binding(f"{channel}:{token}")
        return (
            {**binding.state, "_hatchery_chat_id": binding.chat_id}
            if binding is not None
            else None
        )


def _user_display_name(user: dict | None) -> str | None:
    if user is None:
        return None
    for field in ("name", "username", "email"):
        value = user.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _inbound_author(inbound: channels.Inbound) -> str:
    return str(
        inbound.state.get("user_id")
        or inbound.state.get("sender")
        or inbound.state.get("author")
        or "unknown"
    )


async def _name_chat(chat_id: str, prompt: str) -> None:
    try:
        async with telemetry.use_chat(chat_id):
            async with ai.experimental_telemetry.span("hatchery.title") as span:
                span.set_attrs({"chat.id": chat_id})
                generated = await topic.generate(prompt)
                span.set_attrs({"braintrust.output_json": json.dumps(generated)})
                if generated and await chats.set_topic(chat_id, generated):
                    await events.append(chat_id, "ui", {"type": "chat.changed"})
    finally:
        telemetry.flush()


async def _classify_chat(
    chat_id: str, prompt: str, metadata: dict, candidates: list[models.Agent]
) -> models.Agent:
    async with telemetry.use_chat(chat_id):
        async with ai.experimental_telemetry.span("hatchery.classify") as span:
            span.set_attrs(
                {"chat.id": chat_id},
                origin=str(metadata.get("origin", "unknown")),
                candidate_count=len(candidates),
            )
            await _emit(chat_id, channels.event(channels.protocol.AGENT_ASSIGNING))
            selected = await classifier.classify(prompt, metadata, candidates)
            span.set_attrs({"agent.id": selected.id})
            assigned = await chats.assign_agent(chat_id, selected.id)
            if assigned is None:
                raise fastapi.HTTPException(404, "unknown chat")
            await _emit(
                chat_id,
                channels.event(
                    channels.protocol.AGENT_ASSIGNED,
                    agent={
                        "id": selected.id,
                        "name": selected.name,
                        "color": selected.color,
                    },
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
    configured_agents = await agents.list_all()
    if not configured_agents:
        configured_agents = [await agents.default()]
    for configured_agent in configured_agents:
        await _scaffold_agent(configured_agent)
    yield
    telemetry.flush()


app = fastapi.FastAPI(title="hatchery", lifespan=lifespan)


def _configured_host(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value if "://" in value else f"//{value}")
    return parsed.hostname


async def _ensure_rotor_deployment(request: fastapi.Request) -> None:
    """Activate only the deployment reached through a promoted app URL."""
    deployment = os.environ.get("VERCEL_DEPLOYMENT_ID")
    if not deployment or os.environ.get("VERCEL_ENV") != "production":
        return
    allowed = {
        host
        for host in (
            _configured_host(os.environ.get("HATCHERY_PUBLIC_URL")),
            _configured_host(os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")),
        )
        if host is not None
    }
    if request.url.hostname not in allowed:
        return
    async with _rotor_promotion_lock:
        store = rotor_runtime.worker.backends.store
        await store.setup()
        if await store.active_deployment() != deployment:
            await rotor_runtime.platform.activate(rotor_runtime.worker)


@app.middleware("http")
async def browser_session(request: fastapi.Request, call_next):
    await _ensure_rotor_deployment(request)
    path = request.url.path
    public = (
        path == "/api/health"
        or path == "/api/cron"
        or path == "/api/rotor/activate"
        or path.startswith("/api/auth/")
        or path.startswith("/channels/")
    )
    user = await auth.current_user(request) if path.startswith("/api/") else None
    request.state.user = user
    if path.startswith("/api/") and not public and user is None:
        return fastapi.responses.JSONResponse(
            {"detail": "sign in required"}, status_code=401
        )
    if (
        path.startswith("/api/")
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and not auth.valid_origin(request)
    ):
        return fastapi.responses.JSONResponse(
            {"detail": "invalid origin"}, status_code=403
        )
    return await call_next(request)


# Local development keeps streams and WebSockets direct to :8000 while Vite
# serves the UI on :3000.
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


@app.post("/api/rotor/activate")
async def activate_rotor(request: fastapi.Request) -> dict[str, bool]:
    """Explicitly transfer dispatcher execution to this Vercel deployment."""
    secret = os.environ.get("ROTOR_RELEASE_SECRET")
    authorization = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise fastapi.HTTPException(401, "invalid release authorization")
    await rotor_runtime.platform.activate(rotor_runtime.worker)
    return {"ok": True}


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
    return {"user": request.state.user}


@app.post("/api/auth/logout")
async def auth_logout(request: fastapi.Request):
    return await auth.logout(request)


@app.get("/api/connections/github")
async def github_connection(request: fastapi.Request) -> dict:
    user = request.state.user
    connection = connections.github_connection(user)
    if connection is None:
        return {"connection": None}
    try:
        await connections.github_token(user["id"], connection.get("installation_id"))
    except connections.ConnectionRequired:
        return {"connection": None}
    return {"connection": connection}


@app.get("/api/connections/github/authorize")
async def authorize_github(request: fastapi.Request):
    user = request.state.user
    return await connections.begin_github(request, user)


@app.get("/api/connections/github/return")
async def github_return(request: fastapi.Request):
    user = request.state.user
    try:
        return await connections.finish_github(user)
    except connections.ConnectionRequired as error:
        raise fastapi.HTTPException(
            409, "GitHub authorization was not completed"
        ) from error


@app.delete("/api/connections/github", status_code=204)
async def disconnect_github(request: fastapi.Request) -> None:
    user = request.state.user
    await connections.disconnect_github(user)


class GitHubRepository(pydantic.BaseModel):
    full_name: str
    installation_id: str
    private: bool


class MemoryRepositorySettings(pydantic.BaseModel):
    configured: bool
    memory_repository: str | None


async def _github_repositories(user_id: str) -> list[GitHubRepository]:
    try:
        return [
            GitHubRepository.model_validate(repository)
            for repository in await connections.github_repositories(user_id)
        ]
    except connections.ConnectionRequired as error:
        raise fastapi.HTTPException(409, str(error)) from error
    except (httpx.HTTPError, RuntimeError) as error:
        raise fastapi.HTTPException(502, "GitHub repositories are unavailable") from error


@app.get("/api/connections/github/repositories")
async def github_repositories(request: fastapi.Request) -> list[GitHubRepository]:
    return await _github_repositories(request.state.user["id"])


@app.get("/api/settings")
async def app_settings() -> MemoryRepositorySettings:
    saved = await settings.get()
    return MemoryRepositorySettings(
        configured=await agent_files.configured(),
        memory_repository=saved.memory_repository,
    )


class UpdateMemoryRepositoryRequest(pydantic.BaseModel):
    repository: str


@app.put("/api/settings/memory-repository")
async def update_memory_repository(
    request: fastapi.Request, update: UpdateMemoryRepositoryRequest
) -> MemoryRepositorySettings:
    repositories = await _github_repositories(request.state.user["id"])
    selected = next(
        (
            repository
            for repository in repositories
            if repository.full_name.casefold() == update.repository.casefold()
        ),
        None,
    )
    if selected is None:
        raise fastapi.HTTPException(422, "Choose a repository installed for Hatchery")
    if not selected.private:
        raise fastapi.HTTPException(422, "The memory repository must be private")
    try:
        await connections.github_app_token(
            selected.full_name, selected.installation_id
        )
    except connections.ConnectionRequired as error:
        raise fastapi.HTTPException(
            409, "Install the Hatchery GitHub app on this repository"
        ) from error

    previous = await settings.get()
    configured = models.AppSettings(
        memory_repository=selected.full_name,
        memory_repository_installation_id=selected.installation_id,
    )
    await settings.save(configured)
    try:
        for agent in await agents.list_all() or [await agents.default()]:
            await _scaffold_agent(agent)
    except Exception as error:
        await settings.save(previous)
        raise fastapi.HTTPException(
            502, "Could not initialize the memory repository"
        ) from error
    return MemoryRepositorySettings(
        configured=True, memory_repository=selected.full_name
    )


@app.get("/api/connections/slack")
async def slack_connection(request: fastapi.Request) -> dict:
    user = request.state.user
    connection = connections.slack_connection(user)
    if connection is None:
        return {"connection": None}
    try:
        await connections.slack_token(user["id"])
    except connections.ConnectionRequired:
        return {"connection": None}
    return {"connection": connection}


@app.get("/api/connections/slack/authorize")
async def authorize_slack(request: fastapi.Request):
    return await connections.begin_slack(request, request.state.user)


@app.get("/api/connections/slack/return")
async def slack_return(request: fastapi.Request):
    try:
        return await connections.finish_slack(request.state.user)
    except connections.ConnectionRequired as error:
        raise fastapi.HTTPException(
            409, "Slack authorization was not completed"
        ) from error


@app.delete("/api/connections/slack", status_code=204)
async def disconnect_slack(request: fastapi.Request) -> None:
    await connections.disconnect_slack(request.state.user)


class AgentWarning(pydantic.BaseModel):
    agent_id: str
    repo: str
    warning: str


@app.get("/api/agents")
async def list_agents() -> list[models.Agent]:
    found = await agents.list_all()
    return found or [await agents.default()]


@app.get("/api/agents/warnings")
async def agent_warnings(request: fastapi.Request) -> list[AgentWarning]:
    user = request.state.user
    warnings = []
    for agent in await agents.list_all() or [await agents.default()]:
        if not agent.repos:
            continue
        repo = agent.repos[0]
        try:
            warning = await connections.github_repo_warning(user["id"], repo)
        except connections.ConnectionRequired:
            warning = f"Connect GitHub to let Hatchery make pull requests to {repo}."
        if warning is None:
            continue
        log.warning(
            "agent main repository lacks Hatchery GitHub access",
            extra={"agent_id": agent.id, "repo": repo, "user_id": user["id"]},
        )
        warnings.append(AgentWarning(agent_id=agent.id, repo=repo, warning=warning))
    return warnings


class CreateAgentRequest(pydantic.BaseModel):
    name: str
    slug: str | None = None
    color: models.AccentColor | None = None

    @pydantic.field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        return name

    @pydantic.field_validator("slug")
    @classmethod
    def valid_slug(cls, slug: str | None) -> str | None:
        return models.Agent.valid_slug(slug) if slug is not None else None


async def _scaffold_agent(agent: models.Agent) -> None:
    if not await agent_files.configured():
        return
    for _ in range(3):
        snapshot = await agent_files.snapshot(agent.slug)
        try:
            await agent_files.scaffold(
                agent.slug,
                operation_id=f"agent-create:{agent.id}",
                expected_revision=snapshot.revision,
            )
            return
        except agent_files.Conflict:
            continue
    raise fastapi.HTTPException(409, "agent storage changed while creating the agent")


@app.post("/api/agents")
async def create_agent(request: CreateAgentRequest) -> models.Agent:
    try:
        created = await agents.create(request.name, request.slug, request.color)
    except agents.SlugExists as error:
        raise fastapi.HTTPException(409, "agent slug already exists") from error
    try:
        await _scaffold_agent(created)
    except Exception:
        await agents.delete(created.id)
        raise
    return created


@app.delete("/api/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str) -> None:
    if any(chat.agent_id == agent_id for chat in await chats.list_all()):
        raise fastapi.HTTPException(409, "agent still has threads")
    agent = await agents.get(agent_id)
    if agent is None:
        raise fastapi.HTTPException(404, "unknown agent")
    if await agent_files.configured():
        for _ in range(3):
            snapshot = await agent_files.snapshot(agent.slug)
            try:
                await agent_files.delete_agent(
                    agent.slug,
                    operation_id=f"agent-delete:{agent.id}",
                    expected_revision=snapshot.revision,
                )
                break
            except agent_files.Conflict:
                continue
        else:
            raise fastapi.HTTPException(409, "agent storage changed while deleting")
    await jobs.delete_for_agent(agent_id)
    await agents.delete(agent_id)


class UpdateAgentRequest(pydantic.BaseModel):
    name: str
    about: str
    color: models.AccentColor | None = None

    @pydantic.field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        return name


@app.patch("/api/agents/{agent_id}")
async def update_agent(agent_id: str, request: UpdateAgentRequest) -> models.Agent:
    agent = await agents.get(agent_id)
    if agent is None:
        raise fastapi.HTTPException(404, "unknown agent")
    values = {**agent.model_dump(), "name": request.name, "about": request.about}
    if request.color is not None:
        values["color"] = request.color
    updated = models.Agent.model_validate(values)
    return await agents.save(updated)


class UpdateAgentResourcesRequest(pydantic.BaseModel):
    repos: list[str]
    resources: list[models.Resource]

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if (
                len(parts) != 2
                or not all(parts)
                or any(part.strip() != part for part in parts)
            ):
                raise ValueError("repos must use owner/repo form")
        return repos


@app.patch("/api/agents/{agent_id}/resources")
async def update_agent_resources(
    agent_id: str, request: UpdateAgentResourcesRequest
) -> models.Agent:
    agent = await agents.get(agent_id)
    if agent is None:
        raise fastapi.HTTPException(404, "unknown agent")
    updated = models.Agent.model_validate(
        {
            **agent.model_dump(),
            "repos": request.repos,
            "resources": request.resources,
        }
    )
    return await agents.save(updated)


class AgentFileMutation(pydantic.BaseModel):
    path: str
    content: str = pydantic.Field(max_length=4 * 1024 * 1024)
    operation_id: str
    expected_revision: str | None = None


class AgentFileDelete(pydantic.BaseModel):
    path: str
    operation_id: str
    expected_revision: str | None = None


async def _file_agent(agent_id: str) -> models.Agent:
    agent = await agents.get(agent_id)
    if agent is None:
        raise fastapi.HTTPException(404, "unknown agent")
    if not await agent_files.configured():
        raise fastapi.HTTPException(503, "agent storage is not configured")
    return agent


def _agent_file_error(error: Exception) -> fastapi.HTTPException:
    if isinstance(error, agent_files.Conflict):
        return fastapi.HTTPException(
            409,
            {
                "message": "Agent files changed. Refresh and retry.",
                "current_revision": error.current_revision,
            },
        )
    if isinstance(error, ValueError):
        return fastapi.HTTPException(422, str(error))
    return fastapi.HTTPException(502, "agent storage is unavailable")


@app.get("/api/agents/{agent_id}/files")
async def list_agent_files(agent_id: str) -> models.AgentFilesSnapshot:
    agent = await _file_agent(agent_id)
    try:
        return await agent_files.snapshot(agent.slug)
    except Exception as error:
        raise _agent_file_error(error) from error


@app.get("/api/agents/{agent_id}/file")
async def read_agent_file(
    agent_id: str, path: str, revision: str | None = None
) -> models.AgentFile:
    agent = await _file_agent(agent_id)
    try:
        found = await agent_files.read(agent.slug, path, revision)
    except Exception as error:
        raise _agent_file_error(error) from error
    if found is None:
        raise fastapi.HTTPException(404, "unknown agent file")
    return found


@app.put("/api/agents/{agent_id}/file")
async def write_agent_file(
    agent_id: str, request: AgentFileMutation
) -> models.AgentFilesSnapshot:
    agent = await _file_agent(agent_id)
    try:
        saved = await agent_files.write(
            agent.slug,
            request.path,
            request.content,
            operation_id=request.operation_id,
            expected_revision=request.expected_revision,
        )
        if request.path.startswith("schedules/"):
            await schedule_runtime.reconcile_agent(agent)
        return saved
    except Exception as error:
        raise _agent_file_error(error) from error


@app.delete("/api/agents/{agent_id}/file")
async def delete_agent_file(
    agent_id: str, request: AgentFileDelete
) -> models.AgentFilesSnapshot:
    agent = await _file_agent(agent_id)
    try:
        saved = await agent_files.delete(
            agent.slug,
            request.path,
            operation_id=request.operation_id,
            expected_revision=request.expected_revision,
        )
        if request.path.startswith("schedules/"):
            await schedule_runtime.reconcile_agent(agent)
        return saved
    except Exception as error:
        raise _agent_file_error(error) from error


@app.get("/api/agents/{agent_id}/schedules")
async def agent_schedules(agent_id: str) -> dict:
    agent = await _file_agent(agent_id)
    try:
        return dataclasses.asdict(await schedule_runtime.status(agent))
    except Exception as error:
        raise _agent_file_error(error) from error


@app.post("/api/agents/{agent_id}/schedules/{name}/{action}")
async def set_agent_schedule(agent_id: str, name: str, action: str) -> dict:
    agent = await _file_agent(agent_id)
    if action not in {"pause", "resume"}:
        raise fastapi.HTTPException(404, "unknown schedule action")
    found = next(
        (
            job
            for job in await jobs.list_for_agent(agent.id, f"agent:{agent.id}")
            if job.name == name
        ),
        None,
    )
    if found is None:
        raise fastapi.HTTPException(404, "unknown schedule")
    await jobs.set_paused(found.id, action == "pause")
    return dataclasses.asdict(await schedule_runtime.status(agent))




class JobResponse(pydantic.BaseModel):
    id: str
    agent_id: str
    author_display_name: str | None = None
    schedule: str
    prompt: str
    paused: bool


class JobRequest(pydantic.BaseModel):
    schedule: str
    prompt: str

    @pydantic.field_validator("schedule")
    @classmethod
    def valid_schedule(cls, schedule: str) -> str:
        return jobs.validate_schedule(schedule)

    @pydantic.field_validator("prompt")
    @classmethod
    def valid_prompt(cls, prompt: str) -> str:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        return prompt


class PauseJobRequest(pydantic.BaseModel):
    paused: bool


async def _owned_job(job_id: str, user_id: str) -> models.Job:
    job = await jobs.get(job_id)
    if job is None or job.owner_id != user_id:
        raise fastapi.HTTPException(404, "unknown job")
    return job


def _job_response(job: models.Job) -> JobResponse:
    return JobResponse.model_validate(job.model_dump())


@app.get("/api/agents/{agent_id}/jobs")
async def list_jobs(agent_id: str, request: fastapi.Request) -> list[JobResponse]:
    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    return [
        _job_response(job)
        for job in await jobs.list_for_agent(agent_id, request.state.user["id"])
    ]


@app.post("/api/agents/{agent_id}/jobs")
async def create_job(
    agent_id: str, body: JobRequest, request: fastapi.Request
) -> JobResponse:
    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    return _job_response(
        await jobs.create(
            agent_id,
            request.state.user["id"],
            body.schedule,
            body.prompt,
            author_display_name=_user_display_name(request.state.user),
        )
    )


@app.put("/api/jobs/{job_id}")
async def update_job(
    job_id: str, body: JobRequest, request: fastapi.Request
) -> JobResponse:
    await _owned_job(job_id, request.state.user["id"])
    updated = await jobs.update(job_id, body.schedule, body.prompt)
    assert updated is not None
    return _job_response(updated)


@app.patch("/api/jobs/{job_id}/pause")
async def pause_job(
    job_id: str, body: PauseJobRequest, request: fastapi.Request
) -> JobResponse:
    await _owned_job(job_id, request.state.user["id"])
    updated = await jobs.set_paused(job_id, body.paused)
    assert updated is not None
    return _job_response(updated)


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, request: fastapi.Request) -> None:
    await _owned_job(job_id, request.state.user["id"])
    await jobs.delete(job_id)


@app.get("/api/cron")
async def cron_heartbeat(request: fastapi.Request) -> dict:
    secret = os.environ.get("CRON_SECRET")
    authorization = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise fastapi.HTTPException(401, "invalid cron authorization")
    if os.environ.get("VERCEL_DEPLOYMENT_ID"):
        await rotor_runtime.platform.maintain(rotor_runtime.worker)
    now = datetime.datetime.now(datetime.UTC)
    await schedule_runtime.reconcile_all()
    await jobs.claim_due(now)
    started = 0
    for execution in await jobs.lease_pending(now):
        chat = await chats.get(execution.chat_id)
        if chat is not None and chat.topic is None:
            _spawn(_name_chat(execution.chat_id, execution.prompt))
        registered = next(
            (
                data.get("run_id")
                for _, data in await events.read(execution.chat_id, "turns")
                if data.get("type") == "turn.started"
                and data.get("turn_id") == execution.turn_id
            ),
            None,
        )
        if isinstance(registered, str):
            await jobs.mark_started(execution, registered)
            continue
        job = await jobs.get(execution.job_id)
        if job is None:
            continue
        await durable.start_turn(
            execution.chat_id,
            "cron",
            turn_id=execution.turn_id,
            actor_user_id=job.owner_id,
        )
        started += 1
    await jobs.cleanup(now)
    return {"ok": True, "started": started}


@app.get("/api/threads")
async def list_chats(request: fastapi.Request) -> list[models.Thread]:
    found = await chats.list_all()
    for chat in found:
        if not chat.trigger.startswith("slack:") or chat.title.startswith("slack:"):
            continue
        title = re.sub(r"^<@[^>]+>\s*", "", chat.title)
        title = html.unescape(" ".join(title.split())).strip()
        chat.title = f"slack: {title[:53]}" if title else "slack: thread"
    return found


class CreateChatRequest(pydantic.BaseModel):
    id: str | None = pydantic.Field(default=None, pattern=r"^(?:chat|thread)_[0-9a-f]{12}$")
    title: str = "new chat"
    agent_id: str | None = None


@app.post("/api/threads")
async def create_chat(
    request: CreateChatRequest, http_request: fastapi.Request
) -> models.Thread:
    user = http_request.state.user
    found = await agents.list_all()
    if not found:
        found = [await agents.default()]
    if request.agent_id is not None and not any(
        agent.id == request.agent_id for agent in found
    ):
        raise fastapi.HTTPException(404, "unknown agent")
    if request.id is not None:
        try:
            return await chats.create_once(
                request.id,
                request.agent_id,
                request.title,
                user_id=user["id"] if user is not None else None,
                author_display_name=_user_display_name(user),
            )
        except ValueError as error:
            raise fastapi.HTTPException(409, str(error)) from error
    return await chats.create(
        request.agent_id,
        request.title,
        user_id=user["id"] if user is not None else None,
        author_display_name=_user_display_name(user),
    )


class ArchiveChatRequest(pydantic.BaseModel):
    archived: bool


@app.patch("/api/threads/{chat_id}/archive")
async def archive_chat(chat_id: str, request: ArchiveChatRequest) -> models.Thread:
    async with turns.run(chat_id):
        chat = await chats.get(chat_id)
        if chat is None:
            raise fastapi.HTTPException(404, "unknown chat")
        if request.archived and await durable.active_turn(chat_id) is not None:
            raise fastapi.HTTPException(409, "thread has an active turn")
        if request.archived and any(
            task.status in {"pending", "running", "attention"}
            for task in await worker.store.list_tasks(chat_id)
        ):
            raise fastapi.HTTPException(409, "thread has active subagents")
        updated = await chats.set_archived(chat_id, request.archived)
        if updated is None:
            raise fastapi.HTTPException(404, "unknown chat")
        await events.append(chat_id, "ui", {"type": "chat.changed"})
        return updated


class AssignChatAgentRequest(pydantic.BaseModel):
    agent_id: str


@app.post("/api/threads/{chat_id}/seen")
async def mark_chat_seen(chat_id: str) -> models.Thread:
    updated = await chats.set_attention(chat_id, None)
    if updated is None:
        raise fastapi.HTTPException(404, "unknown chat")
    await events.append(chat_id, "ui", {"type": "chat.changed"})
    return updated


@app.patch("/api/threads/{chat_id}/agent")
async def assign_chat_agent(
    chat_id: str, request: AssignChatAgentRequest
) -> models.Thread:
    if await agents.get(request.agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    # TODO: add history for agent changes
    return await chats.assign_agent(chat_id, request.agent_id)


@app.get("/api/threads/{chat_id}/events")
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
                    index, event = await asyncio.wait_for(
                        asyncio.shield(pending), timeout=30
                    )
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


@app.get("/api/threads/{chat_id}/messages")
async def chat_messages(chat_id: str) -> list[ai.ui.ai_sdk.UIMessage]:
    """The stored transcript as UI messages, with internal messages hidden."""
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    transcript = [
        message
        for message in await _transcript(chat_id)
        if (message.provider_metadata or {}).get("hatchery", {}).get("kind")
        != "subagent_result"
    ]
    messages = ai.ui.ai_sdk.to_ui_messages(transcript)
    metadata = {
        message.id: (message.provider_metadata or {}).get("hatchery", {})
        for message in transcript
    }
    for message in messages:
        if message.role != "user":
            continue
        source = metadata.get(message.id, {})
        if source.get("origin"):
            message.metadata = {
                key: source[key] for key in ("origin", "author") if key in source
            }
        for part in message.parts:
            if getattr(part, "type", None) != "text":
                continue
            if "display_text" in source:
                part.text = source["display_text"]
                continue
            for tag, origin in (
                ("slack_message", "slack"),
                ("github_context", "github"),
            ):
                match = re.fullmatch(
                    rf"<{tag}\b[^>]*>\s*(.*?)\s*</{tag}>", part.text, re.DOTALL
                )
                if match:
                    part.text = (
                        html.unescape(match.group(1))
                        if origin == "slack"
                        else match.group(1)
                    )
                    message.metadata = {**(message.metadata or {}), "origin": origin}
                    break
    return messages


class ChatRequest(pydantic.BaseModel):
    chat_id: str
    messages: list[ai.ui.ai_sdk.UIMessage]


@app.post("/api/chat")
async def chat(
    request: ChatRequest, http_request: fastapi.Request
) -> fastapi.responses.StreamingResponse:
    """Persist user input, enqueue a Rotor turn, and attach to its live stream."""
    incoming, _ = ai.ui.ai_sdk.to_messages(request.messages)
    async with turns.run(request.chat_id):
        current = await chats.get(request.chat_id)
        if current is None:
            raise fastapi.HTTPException(404, "unknown chat")
        user = http_request.state.user
        if current.archived_at is not None:
            raise fastapi.HTTPException(
                409, "chat is archived; unarchive it before posting"
            )
        stored = await _transcript(request.chat_id)
        known = {message.id for message in stored}
        received = []
        author = _user_display_name(user) or "User"
        for message in incoming:
            if message.role == "user" and message.id not in known:
                message.provider_metadata = {
                    **(message.provider_metadata or {}),
                    "hatchery": {
                        "origin": "ui",
                        "author": author,
                        "actor_user_id": user["id"],
                    },
                }
                await events.append(
                    request.chat_id,
                    "messages",
                    message.model_dump(mode="json"),
                )
                stored.append(message)
                known.add(message.id)
                received.append(message)
        slack_attribution = {}
        if received:
            identity = user.get("slack") or {}
            if identity.get("team_id") and identity.get("user_id"):
                slack_attribution = {
                    "slack_team_id": identity["team_id"],
                    "slack_user_id": identity["user_id"],
                }
        for message in received:
            await _emit(
                request.chat_id,
                channels.event(
                    channels.protocol.MESSAGE_RECEIVED,
                    message=message.text,
                    message_id=message.id,
                    origin="ui",
                    author=author,
                    **slack_attribution,
                ),
            )

        if received and current.topic is None:
            first = next(
                (message for message in stored if message.role == "user"), None
            )
            if first is not None:
                _spawn(_name_chat(request.chat_id, first.text))
        if current.agent_id is None:
            first = next(
                (message for message in stored if message.role == "user"), None
            )
            if first is None:
                raise fastapi.HTTPException(409, "chat has no first prompt")
            await _classify_chat(
                request.chat_id,
                first.text,
                {"origin": "ui", "author": "current user"},
                await agents.list_all() or [await agents.default()],
            )
        request_message = next(
            (message for message in reversed(incoming) if message.role == "user"),
            None,
        )
        if request_message is None:
            raise fastapi.HTTPException(409, "chat request has no user message")
        turn_id = "turn_" + hashlib.sha256(
            f"ui:{request.chat_id}:{request_message.id}".encode()
        ).hexdigest()
        turn = await durable.start_turn(
            request.chat_id,
            "ui",
            turn_id=turn_id,
            actor_user_id=user["id"],
        )
    return fastapi.responses.StreamingResponse(
        agent_stream.to_sse(turn.run_id, turn.turn_id),
        headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS,
    )


@app.get("/api/chat/{chat_id}/stream")
async def resume_chat_stream(chat_id: str):
    """Replay and tail the active durable turn, or return 204 while idle."""
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    turn = await durable.active_turn(chat_id)
    if turn is None:
        return fastapi.Response(status_code=204)
    return fastapi.responses.StreamingResponse(
        agent_stream.to_sse(turn.run_id, turn.turn_id),
        headers=ai.ui.ai_sdk.UI_MESSAGE_STREAM_HEADERS,
    )


async def _agent_for_chat(chat_id: str) -> models.Agent:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    if chat.agent_id is None:
        raise fastapi.HTTPException(409, "chat has no agent")
    agent = await agents.get(chat.agent_id)
    if agent is None:
        raise RuntimeError(f"chat {chat_id} belongs to unknown agent {chat.agent_id}")
    return agent


async def _transcript(chat_id: str) -> list[ai.messages.Message]:
    stored = [
        ai.messages.Message.model_validate(data)
        for _, data in await events.read(chat_id, "messages")
    ]
    return _dedupe_tool_history(stored)


def _dedupe_tool_history(
    messages: list[ai.messages.Message],
) -> list[ai.messages.Message]:
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
                if (
                    part.tool_call_id in seen_results
                    or part.tool_call_id not in seen_calls
                ):
                    continue
                seen_results.add(part.tool_call_id)
            parts.append(part)
        if parts:
            repaired.append(
                message
                if len(parts) == len(message.parts)
                else message.model_copy(update={"parts": parts})
            )
    return repaired


@app.get("/api/sandboxes/suggestion")
async def suggest_draft_sandbox(agent_id: str | None = None) -> sandbox.Launch:
    agent = await agents.get(agent_id) if agent_id else None
    if agent_id is not None and agent is None:
        raise fastapi.HTTPException(404, "unknown agent")
    if agent is None:
        found = await agents.list_all()
        agent = found[0] if found else await agents.default()
    async with ai.experimental_telemetry.span("sandbox.suggest") as span:
        span.set_attrs({"agent.id": agent.id})
        return await sandbox.suggest(agent)


@app.get("/api/threads/{chat_id}/sandboxes/suggestion")
async def suggest_chat_sandbox(chat_id: str) -> sandbox.Launch:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    agent = await agents.get(chat.agent_id) if chat.agent_id else None
    if agent is None:
        found = await agents.list_all()
        agent = found[0] if found else await agents.default()
    async with telemetry.use_chat(chat_id):
        async with ai.experimental_telemetry.span("sandbox.suggest") as span:
            span.set_attrs({"chat.id": chat_id, "agent.id": agent.id})
            return await sandbox.suggest(agent)


@app.post("/api/threads/{chat_id}/sandboxes")
async def create_chat_sandbox(
    chat_id: str, request: sandbox.Launch, http_request: fastapi.Request
) -> dict:
    async with turns.run(chat_id):
        chat = await chats.get(chat_id)
        if chat is None:
            raise fastapi.HTTPException(404, "unknown chat")
        if chat.archived_at is not None:
            raise fastapi.HTTPException(
                409, "chat is archived; unarchive it before creating a sandbox"
            )
        try:
            created = await sandbox.create(
                chat_id, request, actor_user_id=http_request.state.user["id"]
            )
        except RuntimeError as error:
            raise fastapi.HTTPException(409, str(error)) from error
        return created.model_dump(exclude={"daemon_token"})


@app.get("/api/threads/{chat_id}/hierarchy")
async def chat_hierarchy(chat_id: str) -> dict:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    return dataclasses.asdict(
        worker_hierarchy.project(chat, await worker.store.list_tasks(chat_id))
    )


@app.get("/api/threads/{chat_id}/sandboxes")
async def chat_sandboxes(chat_id: str) -> list[dict]:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    tasks = await worker.store.list_tasks(chat_id)
    terminals = await worker.list_terminals(chat_id)
    sandboxes = await sandbox.list_all(chat_id)
    liveness = await asyncio.gather(
        *(worker.sandbox.is_live(item.sandbox_name) for item in sandboxes)
    )
    result = []
    for item, live in zip(sandboxes, liveness, strict=True):
        data = item.model_dump(exclude={"daemon_token"})
        data["live"] = live
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


@app.post("/api/threads/{chat_id}/sandboxes/{sandbox_id}/terminals", status_code=201)
async def create_manual_terminal(chat_id: str, sandbox_id: str) -> dict:
    try:
        terminal = await worker.create_terminal(chat_id, sandbox_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise fastapi.HTTPException(409, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})
    return {
        **terminal.model_dump(),
        "sandbox_id": terminal.worker_id,
        "session_id": terminal.id,
    }


@app.delete("/api/threads/{chat_id}/terminals/{terminal_id}", status_code=204)
async def delete_manual_terminal(chat_id: str, terminal_id: str) -> None:
    try:
        await worker.delete_terminal(chat_id, terminal_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


@app.delete("/api/threads/{chat_id}/subagents/{subagent_id}", status_code=204)
async def delete_subagent(
    chat_id: str, subagent_id: str, request: fastapi.Request
) -> None:
    try:
        await worker.delete_task(
            chat_id, subagent_id, actor_user_id=request.state.user["id"]
        )
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


@app.delete("/api/threads/{chat_id}/sandboxes/{sandbox_id}", status_code=204)
async def delete_chat_sandbox(chat_id: str, sandbox_id: str) -> None:
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    try:
        await sandbox.destroy(chat_id, sandbox_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error


async def _authenticate_websocket(ws: fastapi.WebSocket) -> dict | None:
    if not auth.valid_origin(ws):
        await ws.accept()
        await ws.close(code=4403, reason="invalid origin")
        return None
    session_id = getattr(ws, "cookies", {}).get(auth.COOKIE, "")
    user = await auth.session_user(session_id)
    if user is not None:
        return user
    await ws.accept()
    await ws.close(code=4401, reason="sign in required")
    return None


async def _prepare_tty(
    ws: fastapi.WebSocket,
    record: worker.Worker,
    chat_id: str,
    session_id: str,
) -> bool:
    try:
        await worker.sandbox.prepare_for_tty(record)
    except Exception as error:
        log.warning(
            "TTY preparation failed chat=%s worker=%s session=%s: %s",
            chat_id,
            record.id,
            session_id,
            error,
            exc_info=True,
        )
        await ws.accept()
        await ws.close(code=1011, reason="sandbox preparation failed")
        return False
    return True


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
        log.warning(
            "TTY bridge failed for sandbox %s session %s: %s",
            record.id,
            session_id,
            error,
        )
        close_code = 1011
        close_reason = "upstream connection failed"
    finally:
        with contextlib.suppress(RuntimeError):
            await ws.close(code=close_code, reason=close_reason)


@app.websocket("/api/threads/{chat_id}/sandboxes/{sandbox_id}/ssh")
async def sandbox_ssh(ws: fastapi.WebSocket, chat_id: str, sandbox_id: str) -> None:
    user = await _authenticate_websocket(ws)
    if user is None:
        return
    record = await worker.get(sandbox_id)
    if record is None or record.chat_id != chat_id:
        await ws.accept()
        await ws.close(code=4404, reason="unknown sandbox")
        return
    await worker.sandbox.prepare_for_command(record, actor_user_id=user["id"])
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
    except fastapi.WebSocketDisconnect, websockets.ConnectionClosed:
        pass
    finally:
        with contextlib.suppress(RuntimeError):
            await ws.close()


@app.get("/api/threads/{chat_id}/subagents/{subagent_id}/readiness")
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


@app.websocket("/api/threads/{chat_id}/subagents/{subagent_id}/tty")
async def task_tty(ws: fastapi.WebSocket, chat_id: str, subagent_id: str) -> None:
    user = await _authenticate_websocket(ws)
    if user is None:
        return
    task = await worker.get_task(chat_id, subagent_id)
    if task is None:
        await ws.accept()
        await ws.close(code=4404, reason="unknown subagent")
        return
    record = await worker.get(task.worker_id)
    if record is None:
        await ws.accept()
        await ws.close(code=4404, reason="unknown sandbox")
        return
    if not await _prepare_tty(ws, record, chat_id, task.id):
        return
    await _bridge_tty(ws, record, task.id)


@app.websocket("/api/threads/{chat_id}/terminals/{terminal_id}/tty")
async def manual_tty(ws: fastapi.WebSocket, chat_id: str, terminal_id: str) -> None:
    user = await _authenticate_websocket(ws)
    if user is None:
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
    if not await _prepare_tty(ws, record, chat_id, terminal.id):
        return
    await _bridge_tty(ws, record, terminal.id, ["/bin/bash", "-l"])


@vercel.queue.subscribe(
    topic=worker_protocol.EVENT_TOPIC,
    consumer_group="hatchery-control-plane-v1",
    max_concurrency=1,
)
async def worker_event(event: worker_protocol.Event) -> None:
    """Persist one at-least-once worker event and wake the owning chat."""
    task = await worker.store.get_task(event.task_id) if event.task_id else None
    if task is not None and event.worker_id != task.worker_id:
        return
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
                            "gen_ai.tool.name": str(
                                event.payload.get("tool_name") or "fx"
                            ),
                            "gen_ai.tool.type": "function",
                            "gen_ai.tool.call.id": str(
                                event.payload.get("tool_call_id") or ""
                            ),
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
                            "gen_ai.tool.call.id": str(
                                event.payload.get("tool_call_id") or ""
                            ),
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
                    question = str(
                        event.payload.get("question") or event.payload.get("text") or ""
                    )
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

        completion_turn_id = "turn_" + hashlib.sha256(
            f"worker:{current.id}:{current.completion_sequence}".encode()
        ).hexdigest()
        await durable.start_turn(
            current.chat_id,
            "worker",
            current.id,
            turn_id=completion_turn_id,
            actor_user_id=current.user_id,
        )


async def _emit(
    chat_id: str, event: channels.Event, *, delivery_key: str | None = None
) -> list[str]:
    async with telemetry.use_chat(chat_id):
        async with ai.experimental_telemetry.span("channel.deliver") as span:
            bindings = await chats.bindings(chat_id)
            delivered = {
                str(data.get("key"))
                for _, data in await events.read(chat_id, "deliveries")
                if data.get("key") is not None
            }
            span.set_attrs(
                {"chat.id": chat_id},
                event_type=event.type,
                binding_count=len(bindings),
            )
            failures = []
            for binding in bindings:
                if binding.token == event.data.get("source_binding"):
                    continue
                channel = bot.channels.get(binding.channel)
                if channel is None:
                    continue
                receipt = (
                    hashlib.sha256(
                        f"{delivery_key}:{event.type}:{binding.token}".encode()
                    ).hexdigest()
                    if delivery_key is not None
                    else None
                )
                if receipt in delivered:
                    continue
                try:
                    await channel.on_event(event, binding.state)
                    if receipt is not None:
                        await events.append(
                            chat_id,
                            "deliveries",
                            {
                                "key": receipt,
                                "delivery_key": delivery_key,
                                "binding": binding.token,
                                "event_type": event.type,
                            },
                        )
                        delivered.add(receipt)
                except Exception as error:
                    log.exception(
                        "channel delivery failed: %s -> %s", chat_id, binding.channel
                    )
                    failures.append(f"{binding.channel}: {error}")
            span.set_attrs(failure_count=len(failures))
            return failures


async def _deliver(
    chat_id: str,
    message: str,
    *,
    final: bool = True,
    delivery_key: str | None = None,
) -> list[str]:
    data = {"message": message}
    if not final:
        data["final"] = False
    if delivery_key is not None:
        data["delivery_key"] = delivery_key
    event = channels.event(channels.protocol.MESSAGE_COMPLETED, **data)
    if delivery_key is not None:
        event.meta.id = "evt_" + hashlib.sha256(delivery_key.encode()).hexdigest()
    return await _emit(chat_id, event, delivery_key=delivery_key)


async def _run_inbound_turn(chat_id: str, actor_user_id: str | None = None) -> None:
    """Start one durable dispatcher turn after the channel has been acknowledged."""
    async with turns.run(chat_id):
        await durable.start_turn(chat_id, "channel", actor_user_id=actor_user_id)


app.include_router(bot.router)

frontend_dist = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "dist"
app.frontend("/", directory=frontend_dist, fallback="index.html")
