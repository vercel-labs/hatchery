"""Chat-scoped Vercel Sandbox control-plane boundary."""

import json

import ai
import pydantic

import models
from store import chats, events
import worker


class Launch(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["title", "repos", "setup_script", "ports", "branch", "git_sha"]},
    )

    title: str = "sandbox"
    repos: list[str] = []
    setup_script: str | None = None
    ports: list[int] = []
    branch: str | None = None
    git_sha: str | None = None

    @pydantic.field_validator("title")
    @classmethod
    def valid_title(cls, title: str) -> str:
        return title.strip()[:80] or "sandbox"

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise ValueError("repos must use owner/repo form")
        return repos

    @pydantic.field_validator("ports")
    @classmethod
    def valid_ports(cls, ports: list[int]) -> list[int]:
        if len(ports) > 4 or any(port < 1 or port > 65535 for port in ports):
            raise ValueError("ports must contain up to four values between 1 and 65535")
        return ports

    @pydantic.model_validator(mode="after")
    def refs_require_repo(self):
        if (self.branch or self.git_sha) and not self.repos:
            raise ValueError("branch and git_sha require a main repo")
        self.setup_script = self.setup_script.strip() if self.setup_script else None
        self.branch = self.branch.strip() if self.branch else None
        self.git_sha = self.git_sha.strip() if self.git_sha else None
        return self


_SYSTEM = """\
Suggest launch parameters for a coding sandbox from the hatchery space below.
Select only relevant owner/repo repositories from the space. The first repo is
primary. Copy an applicable recommended setup script verbatim; otherwise omit
it. Expose only ports the described project is likely to use, at most four.
Use a short plain title. Omit branch and git_sha unless the description names
them explicitly. Return only the requested structured output."""


async def suggest(space: models.Space) -> Launch:
    request = json.dumps(
        {
            "name": space.name,
            "description": space.about,
            "repositories": space.repos,
            "resources": [resource.model_dump() for resource in space.resources],
        },
        ensure_ascii=False,
    )
    agent = ai.Agent()
    async with agent.run(
        ai.get_model("openai/gpt-5.6-luna"),
        [ai.system_message(_SYSTEM), ai.user_message(request)],
        output_type=Launch,
        params=ai.InferenceRequestParams(
            sampling={ai.TemperatureSamplerParams: ai.TemperatureSamplerParams(temperature=0)},
            output=ai.OutputParams(max_tokens=4096),
        ),
    ) as result:
        async for _ in result:
            pass
        return result.output


async def create(chat_id: str, launch: Launch) -> worker.Worker:
    chat = await chats.get(chat_id)
    created = await worker.create(
        chat_id,
        worker.WorkerSpec(**launch.model_dump()),
        user_id=chat.user_id if chat is not None else None,
    )
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})
    return created


async def list_all(chat_id: str) -> list[worker.Worker]:
    return await worker.list_all(chat_id)


async def destroy(chat_id: str, sandbox_id: str) -> None:
    record = await worker.get(sandbox_id)
    if record is None or record.chat_id != chat_id:
        raise ValueError("sandbox does not belong to this chat")
    await worker.destroy(sandbox_id)
    await events.append(chat_id, "ui", {"type": "sandbox.changed"})


async def launch_task(chat_id: str, sandbox_id: str, prompt: str, model: str) -> worker.Task:
    task = await worker.launch_task(chat_id, sandbox_id, prompt, model)
    await events.append(
        chat_id,
        "ui",
        {
            "type": "task.changed",
            "subagent_id": task.id,
            "sandbox_id": task.worker_id,
            "state": task.status,
        },
    )
    return task


async def send_task_input(chat_id: str, task_id: str, prompt: str) -> worker.Task:
    return await worker.send_task_input(chat_id, task_id, prompt)


async def cancel_task(chat_id: str, task_id: str) -> worker.Task:
    return await worker.cancel_task(chat_id, task_id)
