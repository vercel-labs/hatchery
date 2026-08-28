"""Future Vercel Sandbox control-plane boundary.

The old DevBox implementation was removed in migration step 1. The functions
below mark the retained architecture surfaces until the Sandbox and Queues
implementations land.
"""

import json

import ai
import pydantic

import models


class Launch(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        extra="forbid",
        json_schema_extra={
            "required": ["title", "repos", "setup_script", "ports", "branch", "git_sha"]
        },
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
            sampling={
                ai.TemperatureSamplerParams: ai.TemperatureSamplerParams(temperature=0)
            },
            output=ai.OutputParams(max_tokens=4096),
        ),
    ) as result:
        async for _ in result:
            pass
        return result.output


def unavailable() -> NotImplementedError:
    return NotImplementedError("Vercel Sandbox control plane is not implemented")


async def create(launch: Launch) -> dict:
    """Create and configure a persistent Vercel Sandbox."""
    raise unavailable()


async def list_all() -> list[dict]:
    """List persisted sandboxes visible to Hatchery."""
    raise unavailable()


async def destroy(sandbox_id: str) -> None:
    """Destroy a sandbox and its retained runtime resources."""
    raise unavailable()


async def launch_task(sandbox_id: str, prompt: str, model: str) -> dict:
    """Enqueue an idempotent fx task launch command."""
    raise unavailable()


async def send_task_input(task_id: str, prompt: str) -> None:
    """Enqueue idempotent input for an existing fx task."""
    raise unavailable()


async def cancel_task(task_id: str) -> None:
    """Enqueue cancellation for an existing fx task."""
    raise unavailable()
