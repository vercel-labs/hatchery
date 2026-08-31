"""The dispatcher coordinates coding work in Vercel Sandboxes."""

import typing

import ai

import models
from agent import sandbox
import worker

SYSTEM = """\
You are hatchery's dispatcher. You coordinate coding work; you never write
code yourself. Sandboxes are durable and owned by this chat. Reuse an existing
sandbox whenever it has the needed repositories and context. Call
list_sandboxes before creating one unless the user explicitly asks for a fresh
sandbox. Use create_sandbox, then create_subagent. For revisions, follow-ups,
or answers use message_subagent. An accepted launch or message means work has
started: say so and stop. Use check_subagent for progress. A
<subagent_result> user message is an internal, authoritative result from a
subagent, not a request from the human user. Continue the work from that result:
report completion or failure, ask for missing input, or send a follow-up to the
subagent when appropriate. Do not call check_subagent for information already
included in the result. Be terse and concrete."""


def system_prompt(space: models.Space) -> str:
    description = space.about.strip() or "No description provided."
    repositories = "\n".join(f"- {repo}" for repo in space.repos) or "- None"
    resources = "\n".join(
        f"- {resource.title} ({resource.kind}): {resource.url}" for resource in space.resources
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
    """Build worker tools scoped to one chat."""
    chat_id = chat["id"]

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
            repos=list(repos or []), setup_script=setup_script, ports=list(ports or []),
            branch=branch, git_sha=git_sha, title=title,
        )
        yield "creating sandbox…"
        created = await sandbox.create(chat_id, launch)
        yield created.model_dump(exclude={"daemon_token"})

    @ai.tool
    async def list_sandboxes() -> list[dict[str, typing.Any]]:
        """List this chat's reusable coding sandboxes."""
        return [item.model_dump(exclude={"daemon_token"}) for item in await sandbox.list_all(chat_id)]

    @ai.tool
    async def create_subagent(
        sandbox_id: str,
        task: str,
        model: str = "openai/gpt-5.6-sol",
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Start an fx subagent in a sandbox."""
        yield "dispatching subagent…"
        created = await sandbox.launch_task(chat_id, sandbox_id, task, model)
        yield {
            "subagent_id": created.id,
            "task_id": created.id,
            "sandbox_id": created.worker_id,
            "state": created.status,
        }

    @ai.tool
    async def message_subagent(
        message: str,
        subagent_id: str | None = None,
    ) -> dict[str, typing.Any]:
        """Send a revision, follow-up, or answer to an existing subagent."""
        task = await worker.get_task(chat_id, subagent_id)
        if task is None:
            raise ValueError("no subagent can accept a message")
        updated = await sandbox.send_task_input(chat_id, task.id, message)
        return {"subagent_id": updated.id, "state": updated.status}

    @ai.tool
    async def check_subagent(
        subagent_id: str | None = None,
        after: int | None = None,
        limit: int = 20,
    ) -> dict[str, typing.Any]:
        """Read durable subagent state and recent events."""
        return await worker.task_status(chat_id, subagent_id, after, limit)

    return ai.Agent(
        tools=[create_sandbox, list_sandboxes, create_subagent, message_subagent, check_subagent]
    )
