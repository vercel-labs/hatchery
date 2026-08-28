"""The dispatcher coordinates coding work in Vercel Sandboxes."""

import typing

import ai

import models
from agent import sandbox

SYSTEM = """\
You are hatchery's dispatcher. You coordinate coding work; you never write
code yourself. The Vercel Sandbox worker layer is being migrated and is not
implemented yet. Do not claim that coding work was launched. Explain this
briefly when a request needs a sandbox or subagent."""


def system_prompt(space: models.Space) -> str:
    description = space.about.strip() or "No description provided."
    repositories = "\n".join(f"- {repo}" for repo in space.repos) or "- None"
    resources = "\n".join(
        f"- {resource.title} ({resource.kind}): {resource.url}"
        for resource in space.resources
    ) or "- None"
    return f"""{SYSTEM}

You are working in this space:
- Name: {space.name}
- ID: {space.id}

Space description:
{description}

Available repositories:
{repositories}

Attached resources:
{resources}"""


def model() -> ai.Model:
    return ai.get_model("openai/gpt-5.6-sol")


def agent_for(chat: dict) -> ai.Agent:
    """Build retained worker tools as explicit migration stubs."""

    @ai.tool
    async def create_sandbox(
        repos: list[str] | None = None,
        setup_script: str | None = None,
        ports: list[int] | None = None,
        branch: str | None = None,
        git_sha: str | None = None,
        title: str = "sandbox",
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Create a persistent coding sandbox for this chat."""
        launch = sandbox.Launch(
            repos=list(repos or []),
            setup_script=setup_script,
            ports=list(ports or []),
            branch=branch,
            git_sha=git_sha,
            title=title,
        )
        if False:
            yield None
        await sandbox.create(launch)

    @ai.tool
    async def list_sandboxes() -> list[dict[str, typing.Any]]:
        """List this chat's reusable coding sandboxes."""
        return await sandbox.list_all()

    @ai.tool
    async def create_subagent(
        sandbox_id: str,
        task: str,
        model: str = "openai/gpt-5.6-sol",
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Start an fx subagent in a sandbox."""
        if False:
            yield None
        await sandbox.launch_task(sandbox_id, task, model)

    @ai.tool
    async def message_subagent(
        message: str,
        subagent_id: str | None = None,
    ) -> dict[str, typing.Any]:
        """Send input to an existing fx subagent."""
        raise sandbox.unavailable()

    @ai.tool
    async def check_subagent(
        subagent_id: str | None = None,
        after: int | None = None,
        limit: int = 20,
    ) -> dict[str, typing.Any]:
        """Read durable subagent state and recent events."""
        raise sandbox.unavailable()

    return ai.Agent(
        tools=[
            create_sandbox,
            list_sandboxes,
            create_subagent,
            message_subagent,
            check_subagent,
        ]
    )
