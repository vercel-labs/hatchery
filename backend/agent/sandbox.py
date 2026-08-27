"""Manual and dispatcher-driven devbox creation."""

import json
import typing

import ai
import pydantic

import models
from agent import devbox
from store import devboxes, events, workspaces


class Launch(pydantic.BaseModel):
    title: str = "devbox"
    repos: list[str] = []
    setup_script: str | None = None
    ports: list[int] = []
    branch: str | None = None
    git_sha: str | None = None

    @pydantic.field_validator("title")
    @classmethod
    def valid_title(cls, title: str) -> str:
        return title.strip()[:80] or "devbox"

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
            sampling={
                ai.TemperatureSamplerParams: ai.TemperatureSamplerParams(temperature=0)
            },
            output=ai.OutputParams(max_tokens=4096),
        ),
    ) as result:
        async for _ in result:
            pass
        return result.output


async def prepare(chat_id: str, launch: Launch) -> dict[str, typing.Any]:
    record = await devboxes.create(chat_id, launch.title, launch.repos)
    record.update(
        setup_script=launch.setup_script,
        ports=launch.ports,
        branch=launch.branch,
        git_sha=launch.git_sha,
    )
    await devboxes.save(record)
    await events.append(chat_id, "ui", {"type": "devbox.changed"})
    return record


async def provision(record: dict[str, typing.Any]) -> dict[str, typing.Any]:
    async with workspaces.provision(record["chat_id"]):
        try:
            record["set_id"] = await devbox.create_taskset(
                f"hatchery {record['chat_id']} / {record['title']}"
            )
            record["box"] = await devbox.create_box(
                f"hatchery-{record['chat_id']}-{record['id'][-6:]}",
                record["repos"],
                setup_script=record.get("setup_script"),
                ports=record.get("ports"),
                branch=record.get("branch"),
                git_sha=record.get("git_sha"),
            )
            record["state"] = "ready"
        except Exception as error:
            record["state"] = "errored"
            record["error"] = str(error)
            await devboxes.save(record)
            await events.append(record["chat_id"], "ui", {"type": "devbox.changed"})
            raise
        await devboxes.save(record)
        await events.append(record["chat_id"], "ui", {"type": "devbox.changed"})
        return record
