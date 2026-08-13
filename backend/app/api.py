"""UI-facing routes (mounted under /api by app.server).

  GET  /api/projects                 list projects
  POST /api/projects                 create a project
  GET  /api/projects/{id}            project + its chats
  PUT  /api/projects/{id}/memory     replace the project memory file
  PUT  /api/projects/{id}/repos      replace the attached repo list
  POST /api/chats                    create a manual chat
  GET  /api/chats/{id}               chat + its full event log
  POST /api/chats/{id}/messages      send a user message, start a turn
  POST /api/chats/{id}/status        archive / reactivate
  GET  /api/chats/{id}/stream        ndjson tail of the event stream

The stream is a poll-tail over the durable store (seal's stance): the client
reconnects with ?start=<next index> when the response ends, so serverless
time limits just look like a reconnect.
"""

import asyncio
import json
import typing

import fastapi
import fastapi.responses
import pydantic

import channels
from channels import protocol
from store import chats, events, projects

POLL_SECONDS = 0.3
STREAM_LIMIT_SECONDS = 110  # end the response before the platform does; clients reconnect


class CreateProject(pydantic.BaseModel):
    name: str


class Memory(pydantic.BaseModel):
    memory: str


class Repos(pydantic.BaseModel):
    repos: list[str]


class CreateChat(pydantic.BaseModel):
    project_id: str
    title: str = "new chat"


class Send(pydantic.BaseModel):
    message: str


class Status(pydantic.BaseModel):
    status: typing.Literal["active", "archived"]


def router(bot: channels.App) -> fastapi.APIRouter:
    api = fastapi.APIRouter(prefix="/api")

    @api.get("/projects")
    async def list_projects() -> list[projects.Project]:
        return await projects.list_projects()

    @api.post("/projects", status_code=201)
    async def create_project(body: CreateProject) -> projects.Project:
        return await projects.create(body.name)

    @api.get("/projects/{project_id}")
    async def get_project(project_id: str) -> dict:
        project = await projects.get(project_id)
        if project is None:
            raise fastapi.HTTPException(404, "project not found")
        chat_list = await chats.list_for_project(project_id)
        return {**project.model_dump(), "chats": [chat.model_dump() for chat in chat_list]}

    @api.put("/projects/{project_id}/memory")
    async def set_memory(project_id: str, body: Memory) -> projects.Project:
        project = await projects.set_memory(project_id, body.memory)
        if project is None:
            raise fastapi.HTTPException(404, "project not found")
        return project

    @api.put("/projects/{project_id}/repos")
    async def set_repos(project_id: str, body: Repos) -> projects.Project:
        project = await projects.set_repos(project_id, body.repos)
        if project is None:
            raise fastapi.HTTPException(404, "project not found")
        return project

    @api.post("/chats", status_code=201)
    async def create_chat(body: CreateChat) -> chats.Chat:
        if await projects.get(body.project_id) is None:
            raise fastapi.HTTPException(404, "project not found")
        return await chats.create(body.project_id, body.title)

    @api.get("/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict:
        chat = await chats.get(chat_id)
        if chat is None:
            raise fastapi.HTTPException(404, "chat not found")
        records = await events.read(chat_id)
        return {
            **chat.model_dump(),
            "events": [{"index": index, **data} for index, data in records],
        }

    @api.post("/chats/{chat_id}/messages")
    async def send_message(chat_id: str, body: Send) -> dict:
        chat = await chats.get(chat_id)
        if chat is None:
            raise fastapi.HTTPException(404, "chat not found")
        await chats.touch(chat_id)
        await bot.emit(chat_id, protocol.event(protocol.MESSAGE_RECEIVED, message=body.message, channel="ui"))
        try:
            await bot.start_turn(chat, body.message)
        except Exception as exc:
            await bot.emit(chat_id, protocol.event(protocol.TURN_FAILED, error=str(exc)))
            raise fastapi.HTTPException(502, f"failed to start turn: {exc}")
        return {"ok": True}

    @api.post("/chats/{chat_id}/status")
    async def set_status(chat_id: str, body: Status) -> chats.Chat:
        chat = await chats.set_status(chat_id, body.status)
        if chat is None:
            raise fastapi.HTTPException(404, "chat not found")
        return chat

    @api.get("/chats/{chat_id}/stream")
    async def stream(chat_id: str, start: int = 0) -> fastapi.responses.StreamingResponse:
        if await chats.get(chat_id) is None:
            raise fastapi.HTTPException(404, "chat not found")

        async def tail() -> typing.AsyncIterator[str]:
            index = start
            for _tick in range(int(STREAM_LIMIT_SECONDS / POLL_SECONDS)):
                for record_index, data in await events.read(chat_id, index):
                    yield json.dumps({"index": record_index, **data}, separators=(",", ":")) + "\n"
                    index = record_index + 1
                await asyncio.sleep(POLL_SECONDS)

        return fastapi.responses.StreamingResponse(tail(), media_type="application/x-ndjson")

    return api
