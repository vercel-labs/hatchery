"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

Health check, channel webhooks, and the thread chat:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/hatchery")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG
- /api/chat            thread turn, AI SDK UI message stream (SSE)

Application projections live in the store (Postgres via DATABASE_URL, local
files without). Rotor checkpoints the canonical thread conversation; the
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
import typing
import urllib.parse

import fastapi
import fastapi.middleware.cors
import fastapi.responses
import pydantic
import rotor
import rotor.stores.base
import websockets.asyncio.client

import ai
import ai.ui.ai_sdk.outbound_stream
import ai.ui.ai_sdk.ui_events
from hatchery import auth
from hatchery import channels
from hatchery import connections
from hatchery import environment
from hatchery import messages
from hatchery import models
from hatchery import store
from hatchery import templates
import vercel.functions
import vercel.queue
from hatchery.agent import classifier, runtime as rotor_runtime, sandbox, stream as agent_stream, supervisor, telemetry, thread, topic
from hatchery import worker
from hatchery.worker import protocol as worker_protocol
from hatchery.channels import github, slack
from hatchery import config
from hatchery.serve import api as serve_api, app as serve_app, host as serve_host
from hatchery.store import agents, chats, events, jobs, turns
from hatchery.workspace import browser as workspace_browser
from hatchery.workspace import git as workspace_git
from hatchery.workspace import review as workspace_review

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
    """Land inbound messages in a chat and run one thread turn.

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
            found = await _agents()
            title = inbound.title or inbound.text.strip().splitlines()[0][:80]
            token = f"{channel}:{inbound.token}"
            if channel == "slack":
                token = f"slack:{inbound.state['team_id']}:{inbound.token}"
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
# Outermost: `<agent>.<HATCHERY_SERVE_DOMAIN>` goes to the agent's published routes
# before session auth; malformed agent hosts fail closed.
app.add_middleware(
    serve_host.HostDispatch,
    serve_app=serve_app.create_serve_app(),
    domain=config.load().serve.domain,
    allow_agent_header=os.environ.get("VERCEL_ENV") == "preview",
)
app.include_router(serve_api.router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "channels": list(bot.channels)}


@app.post("/api/rotor/activate")
async def activate_rotor(request: fastapi.Request) -> dict[str, bool]:
    """Explicitly transfer Rotor execution to this Vercel deployment."""
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
    return await _agents()


@app.get("/api/agents/warnings")
async def agent_warnings(request: fastapi.Request) -> list[AgentWarning]:
    user = request.state.user
    warnings = []
    for agent in await _agents():
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
    id: str | None = None
    color: models.AccentColor | None = None

    @pydantic.field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        return name

    @pydantic.field_validator("id")
    @classmethod
    def valid_id(cls, agent_id: str | None) -> str | None:
        return None if agent_id is None else agents.validate_slug(agent_id)


@app.post("/api/agents")
async def create_agent(request: CreateAgentRequest) -> models.Agent:
    """Register the agent, then commit its template directory to the storage repo.

    A failed commit removes the row again, so the same request can simply be retried.
    A taken ID is 409.
    """
    try:
        agent = await agents.create(request.name, request.id, request.color)
    except agents.Taken as error:
        raise fastapi.HTTPException(409, f"agent id {error} is taken") from error
    except ValueError as error:
        raise fastapi.HTTPException(422, "cannot derive an id from this name; pass an id") from error
    try:
        return await _join(agent)
    except Exception as error:
        raise fastapi.HTTPException(502, "could not create the agent workspace") from error


async def _join(agent: models.Agent) -> models.Agent:
    """Commit a new agent's template directory to the storage repo (agentmesh `join`).

    A failed commit removes the row again, so the caller can simply retry. A retry
    after a commit that did land finds the directory and succeeds.
    """
    env = rotor_runtime.install()
    if env is None:
        return agent
    try:
        await env.workspaces.join(agent.id, templates.load())
    except FileExistsError:
        pass  # a retried create already committed it
    except Exception:
        await agents.delete(agent.id)
        log.exception("could not commit the template for agent %s", agent.id)
        raise
    return agent


async def _agents() -> list[models.Agent]:
    """All agents. A fresh deployment first seeds the default agent the way create does."""
    return await agents.list_all() or [await _join(await agents.default())]


@app.delete("/api/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str) -> None:
    if any(chat.agent_id == agent_id for chat in await chats.list_all()):
        raise fastapi.HTTPException(409, "agent still has chats")
    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    await jobs.delete_for_agent(agent_id)
    await agents.delete(agent_id)


class UpdateAgentRequest(pydantic.BaseModel):
    name: str
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
    values = {**agent.model_dump(), "name": request.name}
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
        await supervisor.start_turn(
            execution.chat_id,
            "cron",
            turn_id=execution.turn_id,
            actor_user_id=job.owner_id,
        )
        started += 1
    await jobs.cleanup(now)
    if now.minute % 5 == 0:
        await serve_api.maintain()  # Git schedules, a separate job kind
    return {"ok": True, "started": started}


@app.get("/api/chats")
async def list_chats(
    request: fastapi.Request, parent_chat_id: str | None = None
) -> list[models.Chat]:
    """Root chats, or with `parent_chat_id` the chats of that chat's delegated threads."""
    return await chats.list_all(parent_chat_id)


class CreateChatRequest(pydantic.BaseModel):
    id: str | None = pydantic.Field(default=None, pattern=r"^chat_[0-9a-f]{12}$")
    title: str = "new chat"
    agent_id: str | None = None


@app.post("/api/chats")
async def create_chat(
    request: CreateChatRequest, http_request: fastapi.Request
) -> models.Chat:
    user = http_request.state.user
    found = await _agents()
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


@app.patch("/api/chats/{chat_id}/archive")
async def archive_chat(chat_id: str, request: ArchiveChatRequest) -> models.Chat:
    async with turns.run(chat_id):
        chat = await chats.get(chat_id)
        if chat is None:
            raise fastapi.HTTPException(404, "unknown chat")
        if request.archived and await supervisor.active_turn(chat_id) is not None:
            raise fastapi.HTTPException(409, "chat has an active turn")
        updated = await chats.set_archived(chat_id, request.archived)
        if updated is None:
            raise fastapi.HTTPException(404, "unknown chat")
        thread_id = await supervisor.thread_for_chat(chat_id)
        if thread_id is not None and chat.agent_id is not None:
            # A root archive cascades through its settled delegated subtree.
            await supervisor.send(
                chat.agent_id, messages.ArchiveThread(thread_id, archived=request.archived)
            )
        await events.append(chat_id, "ui", {"type": "chat.changed"})
        return updated


class AssignChatAgentRequest(pydantic.BaseModel):
    agent_id: str


@app.post("/api/chats/{chat_id}/seen")
async def mark_chat_seen(chat_id: str) -> models.Chat:
    updated = await chats.set_attention(chat_id, None)
    if updated is None:
        raise fastapi.HTTPException(404, "unknown chat")
    await events.append(chat_id, "ui", {"type": "chat.changed"})
    return updated


@app.patch("/api/chats/{chat_id}/agent")
async def assign_chat_agent(
    chat_id: str, request: AssignChatAgentRequest
) -> models.Chat:
    if await agents.get(request.agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    if await supervisor.thread_for_chat(chat_id) is not None:
        raise fastapi.HTTPException(409, "the chat's thread has started; its agent is fixed")
    # TODO: add history for agent changes
    return await chats.assign_agent(chat_id, request.agent_id)


async def _chat_thread(chat_id: str) -> tuple[models.Chat, str]:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    thread_id = await supervisor.thread_for_chat(chat_id)
    if thread_id is None or chat.agent_id is None:
        raise fastapi.HTTPException(404, "the chat has no thread yet")
    return chat, thread_id


@app.get("/api/chats/{chat_id}/thread")
async def chat_thread(chat_id: str) -> dict:
    """The chat's thread: status, branch, proposals, tasks, tokens, and model history."""
    _, thread_id = await _chat_thread(chat_id)
    try:
        snapshot = await rotor_runtime.client.snapshot(thread_id)
        value, revision = await rotor_runtime.client.query(
            thread_id, thread.AgentThread.details
        )
    except rotor.ProcessNotFound as error:
        raise fastapi.HTTPException(404, "the thread has not started yet") from error
    if snapshot.phase == "terminal":
        value["status"] = "done" if snapshot.terminal_status == "completed" else "failed"
    await _refresh_proposals(value["proposals"])
    return {
        **value,
        "live": snapshot.phase != "terminal",
        "revision": revision,
        "activity": _runtime_activity(value["activity"], snapshot),
    }


def _runtime_activity(value: dict, snapshot: rotor.stores.base.Snapshot) -> dict:
    """Combine the thread's committed work with Rotor's current execution and mailbox."""
    terminal = snapshot.terminal_status
    return {
        **value,
        "status": ("done" if terminal == "completed" else terminal) or value["status"],
        "phase": snapshot.phase,
        "mailbox_depth": snapshot.mailbox_depth,
        "schedules": snapshot.schedules,
        "updated_at": snapshot.updated_at,
    }


async def _refresh_proposals(proposals: list[dict]) -> None:
    """Git ancestry decides `merged`, even after the thread that proposed has stopped."""
    env = rotor_runtime.install()
    open_branches = [p["branch"] for p in proposals if not p["merged"]]
    if env is None or not open_branches:
        return
    merged = await env.workspaces.merged_proposals(open_branches)
    for proposal in proposals:
        if proposal["branch"] in merged:
            proposal["merged"] = merged[proposal["branch"]]


@app.get("/api/repository")
async def repository(
    chat_id: str | None = None,
    proposal: str | None = None,
    path: str | None = None,
    revision: str | None = None,
    comparison: typing.Literal["full", "latest"] = "full",
) -> dict:
    """Browse committed `main`, a chat thread's branch, or one of its proposals.

    With `path`, one file's before/after preview and diff; without, the tree and changes.
    """
    env = rotor_runtime.install()
    if env is None:
        raise fastapi.HTTPException(409, "storage is not configured")
    branch, base = "main", None
    if chat_id is not None:
        _, thread_id = await _chat_thread(chat_id)
        try:
            detail, _ = await rotor_runtime.client.query(thread_id, thread.AgentThread.details)
        except rotor.ProcessNotFound as error:
            raise fastapi.HTTPException(404, "the thread has not started yet") from error
        branch = detail["branch"]
        base = (detail["base_sha"] or None) if comparison == "full" else None
        if proposal is not None:
            if not any(p["branch"] == proposal for p in detail["proposals"]):
                raise fastapi.HTTPException(404, "proposal is not recorded on this thread")
            if comparison == "latest":
                raise fastapi.HTTPException(
                    422, "latest comparison is only available for thread edits"
                )
            branch = proposal
    elif proposal is not None:
        raise fastapi.HTTPException(422, "a proposal requires its chat_id")
    elif comparison == "latest":
        raise fastapi.HTTPException(422, "latest comparison requires a chat_id")
    try:
        view = await workspace_browser.RepositoryBrowser(env.workspaces).inspect(
            branch, base=base, path=path, revision=revision
        )
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error
    except FileNotFoundError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    except workspace_git.GitError as error:
        raise fastapi.HTTPException(409, str(error)) from error
    return {
        **view,
        "remote": env.workspaces.remote,
        "review": env.config.review.model_dump(),
        "local_review": isinstance(env.review, workspace_review.LocalReview),
        "checkout": None,
    }


async def _thread_sandbox(chat_id: str) -> tuple[environment.Environment, str]:
    env = rotor_runtime.install()
    if env is None:
        raise fastapi.HTTPException(409, "storage is not configured")
    _, thread_id = await _chat_thread(chat_id)
    try:
        detail, _ = await rotor_runtime.client.query(thread_id, thread.AgentThread.details)
    except rotor.ProcessNotFound as error:
        raise fastapi.HTTPException(404, "the thread has not started yet") from error
    name = detail.get("sandbox")
    if not isinstance(name, str) or not name:
        raise fastapi.HTTPException(404, "thread sandbox is unavailable")
    return env, name


@app.get("/api/chats/{chat_id}/thread/filesystem")
async def thread_directory(chat_id: str, path: str = "") -> dict:
    """List one directory of the thread sandbox's live `/workspace`."""
    env, name = await _thread_sandbox(chat_id)
    try:
        view = await env.sandboxes.list_directory(name, path)
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error
    except FileNotFoundError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    return dataclasses.asdict(view)


@app.get("/api/chats/{chat_id}/thread/filesystem/file")
async def thread_file(chat_id: str, path: str) -> dict:
    """Preview a bounded regular file from the thread sandbox's live `/workspace`."""
    env, name = await _thread_sandbox(chat_id)
    try:
        preview = await env.sandboxes.read_file(
            name, path, limit=workspace_browser.PREVIEW_BYTES
        )
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error
    except FileNotFoundError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    try:
        text = preview.content.decode("utf-8")
    except UnicodeDecodeError as error:
        # Only a truncated final UTF-8 character is tolerated.
        cut = (
            preview.truncated
            and error.end == len(preview.content)
            and error.reason == "unexpected end of data"
        )
        text = preview.content[: error.start].decode("utf-8") if cut else None
    if text is not None and "\0" in text:
        text = None
    return {
        "path": preview.path,
        "text": text,
        "notice": "Binary file; preview unavailable"
        if text is None
        else "Preview truncated at 256 KiB"
        if preview.truncated
        else None,
        "truncated": preview.truncated,
        "bytes_read": len(preview.content),
    }


class ThreadCommand(pydantic.BaseModel):
    request_id: str = pydantic.Field(min_length=1, max_length=200)


@app.post("/api/chats/{chat_id}/thread/resume", status_code=202)
async def resume_thread(chat_id: str, request: ThreadCommand) -> dict:
    """Resume a parked thread; a turn-ceiling park gets one more window of turns."""
    _, thread_id = await _chat_thread(chat_id)
    outcome = await rotor_runtime.client.send(
        thread_id, messages.Resume(), idempotency_key=f"resume:{request.request_id}"
    )
    return {"request_id": request.request_id, "outcome": outcome}


@app.post("/api/chats/{chat_id}/thread/stop", status_code=202)
async def stop_thread(chat_id: str, request: ThreadCommand) -> dict:
    """Interrupt the thread's current work; it stays live and resumable."""
    _, thread_id = await _chat_thread(chat_id)
    outcome = await rotor_runtime.client.send(
        thread_id,
        messages.Stop(),
        idempotency_key=f"stop:{request.request_id}",
        preempt=True,
    )
    return {"request_id": request.request_id, "outcome": outcome}


class ApproveRequest(pydantic.BaseModel):
    branch: str
    expected_sha: str = pydantic.Field(pattern=r"^[0-9a-f]{40}$")


@app.post("/api/chats/{chat_id}/thread/approve")
async def approve_proposal(chat_id: str, request: ApproveRequest) -> dict:
    """Merge a root thread's reviewed proposal at exactly the reviewed commit."""
    _, thread_id = await _chat_thread(chat_id)
    env = rotor_runtime.install()
    if env is None:
        raise fastapi.HTTPException(409, "storage is not configured")
    value, _ = await rotor_runtime.client.query(thread_id, thread.AgentThread.details)
    if value["parent_thread_id"]:
        raise fastapi.HTTPException(409, "the parent thread reviews this proposal")
    if not any(p["branch"] == request.branch for p in value["proposals"]):
        raise fastapi.HTTPException(404, "proposal is not recorded on this thread")
    if not isinstance(env.review, workspace_review.LocalReview):
        raise fastapi.HTTPException(409, "review this proposal on GitHub")
    try:
        sha = await env.review.merge(request.branch, expected_sha=request.expected_sha)
    except workspace_git.MergeConflict as error:
        raise fastapi.HTTPException(409, f"{error}. Prompt the thread to re-merge.") from error
    return {"branch": request.branch, "commit": sha}


@app.get("/api/agents/{agent_id}/threads")
async def agent_threads(agent_id: str) -> dict:
    """The agent's thread tree, tasks, waiting threads, and daily budget."""
    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    roster = await supervisor.roster(agent_id)
    # The supervisor's last report can stay idle through a background activation, so
    # read live execution directly, bounded and without loading journals.
    reads = asyncio.Semaphore(8)

    async def enrich(item: dict) -> None:
        async with reads:
            try:
                snapshot = await rotor_runtime.client.snapshot(item["thread_id"])
                value, _ = await rotor_runtime.client.query(
                    item["thread_id"], thread.AgentThread.activity
                )
            except rotor.ProcessNotFound:
                return  # terminal process retention may expire before its report
            item["activity"] = _runtime_activity(value, snapshot)

    threads = roster.get("threads", [])
    await asyncio.gather(*(enrich(item) for item in threads if item.get("live")))
    await _refresh_proposals([p for item in threads for p in item.get("proposals", [])])
    env = rotor_runtime.install()
    return {
        **roster,
        "local_review": env is not None
        and isinstance(env.review, workspace_review.LocalReview),
    }


class GrantRequest(pydantic.BaseModel):
    request_id: str = pydantic.Field(min_length=1, max_length=200)
    amount: int = pydantic.Field(gt=0)


@app.post("/api/agents/{agent_id}/grants", status_code=202)
async def grant_budget(agent_id: str, request: GrantRequest) -> dict:
    """Add tokens to today's budget; held threads resume."""
    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    outcome = await supervisor.send(
        agent_id,
        messages.Grant(request.request_id, request.amount),
        idempotency_key=f"grant:{request.request_id}",
    )
    return {"request_id": request.request_id, "outcome": outcome}


@app.post("/api/agents/{agent_id}/retire", status_code=202)
async def retire_agent(agent_id: str, request: ThreadCommand) -> dict:
    """Cancel every thread; each sandbox gets a final checkpoint and is stopped, not deleted.

    Serving is torn down first: the vault is cleared, the serve sandbox and its data are
    destroyed, schedules are cancelled, and the agent's host answers 410 from then on.
    """
    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    await serve_api.retire(agent_id, request.request_id)
    outcome = await supervisor.send(
        agent_id, messages.Retire(), idempotency_key=f"retire:{request.request_id}"
    )
    return {"request_id": request.request_id, "outcome": outcome}


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


TRANSCRIPT_METADATA = (
    "timestamp",
    "source",
    "request_id",
    "turn",
    "task_id",
    "task_handle",
    "reporting_thread_id",
    "task_status",
    "task_event",
)


@app.get("/api/chats/{chat_id}/transcript")
async def chat_transcript(chat_id: str) -> list[dict]:
    """The stored transcript as model messages with time and provenance (the console).

    A turn's steps stay separate messages and each keeps the metadata the thread
    stored beside it: timestamp, source, task and request IDs.
    """
    if await chats.get(chat_id) is None:
        raise fastapi.HTTPException(404, "unknown chat")
    raw = {data.get("id"): data for _, data in await events.read(chat_id, "messages")}
    found = []
    for message in await _transcript(chat_id):
        source = (message.provider_metadata or {}).get("hatchery", {})
        if source.get("kind") == "subagent_result":
            continue
        stored = raw.get(message.id, {})
        item = {
            **message.model_dump(mode="json", exclude={"provider_metadata", "usage"}),
            **{key: stored[key] for key in TRANSCRIPT_METADATA if key in stored},
            **{key: source[key] for key in ("origin", "author") if key in source},
        }
        if message.role == "user":
            for part in item["parts"]:
                if part.get("kind") != "text":
                    continue
                if "display_text" in source:
                    part["text"] = source["display_text"]
                    continue
                for tag, origin in (("slack_message", "slack"), ("github_context", "github")):
                    match = re.fullmatch(
                        rf"<{tag}\b[^>]*>\s*(.*?)\s*</{tag}>", part["text"], re.DOTALL
                    )
                    if match:
                        text = match.group(1)
                        part["text"] = html.unescape(text) if origin == "slack" else text
                        item["origin"] = origin
                        break
        found.append(item)
    return found


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
                await _agents(),
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
        turn = await supervisor.start_turn(
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
    turn = await supervisor.active_turn(chat_id)
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
    return [
        ai.messages.Message.model_validate(data)
        for _, data in await events.read(chat_id, "messages")
    ]


@app.get("/api/chats/{chat_id}/sandboxes")
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


@app.post("/api/chats/{chat_id}/sandboxes/{sandbox_id}/terminals", status_code=201)
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


@app.delete("/api/chats/{chat_id}/terminals/{terminal_id}", status_code=204)
async def delete_manual_terminal(chat_id: str, terminal_id: str) -> None:
    try:
        await worker.delete_terminal(chat_id, terminal_id)
    except ValueError as error:
        raise fastapi.HTTPException(404, str(error)) from error
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


@app.delete("/api/chats/{chat_id}/subagents/{subagent_id}", status_code=204)
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


@app.delete("/api/chats/{chat_id}/sandboxes/{sandbox_id}", status_code=204)
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


@app.websocket("/api/chats/{chat_id}/sandboxes/{sandbox_id}/ssh")
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


@app.websocket("/api/chats/{chat_id}/terminals/{terminal_id}/tty")
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
    """Persist one at-least-once worker event and wake the owning chat.

    Events from a worker without a record are dropped, e.g. sandboxes started
    before the agents cutover (their tasks remain in the same table).
    """
    if await worker.store.get(event.worker_id) is None:
        return
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
    """Persist one internal result, then let the thread continue the chat."""
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
        await supervisor.start_turn(
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
    """Start one agent thread turn after the channel has been acknowledged."""
    async with turns.run(chat_id):
        await supervisor.start_turn(chat_id, "channel", actor_user_id=actor_user_id)


app.include_router(bot.router)

frontend_dist = pathlib.Path(__file__).resolve().parents[1] / "static"
app.frontend("/", directory=frontend_dist, fallback="index.html")
