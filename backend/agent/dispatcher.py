"""The dispatcher: coordinates chat-owned devboxes and their subagents."""

import secrets
import typing

import ai
from vercel import workflow

import models
from agent import devbox, supervisor
from store import activity, chats, devboxes, events, subagents, workspaces

SYSTEM = """\
You are hatchery's dispatcher. You coordinate coding work; you never write
code yourself. Devboxes are durable sandboxes owned by this chat. Reuse an
existing devbox whenever it has the needed repositories and context. Call
list_devboxes before creating one unless the user explicitly asks for a fresh
sandbox. If the space description has a "Recommended devbox setup" section,
pass its shell commands verbatim as setup_script when they apply; otherwise
omit setup_script. You may compose a freeform setup script when the sandbox
needs other tools. Use create_devbox to create a sandbox, then create_subagent
with its ID to start coding or investigation. A devbox can host many subagents.
While a subagent runs the user watches its terminal live, so don't narrate its
steps. A deployed launch only means the task was accepted: reply only that work
has started and stop. Use check_subagent for progress or when woken by state
changes. Never create another subagent merely to check one. If it fails, say so
plainly and stop. Be terse and concrete."""


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


def agent_for(
    chat: dict,
    on_task_created: typing.Callable[[dict, dict], None] | None = None,
) -> ai.Agent:
    """Build the dispatcher tools scoped to one chat."""

    @ai.tool
    async def create_devbox(
        repos: list[str] | None = None,
        setup_script: str | None = None,
        title: str = "devbox",
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Create a durable sandbox owned by this chat.

        Select only the owner/repo repositories needed in the sandbox. Omit
        repos for an empty sandbox. setup_script is freeform shell run once,
        after cloning and before any subagent starts. Copy applicable recommended
        setup from the space description verbatim. Prefer reusing a suitable
        listed devbox.
        """
        desired_repos = list(repos or [])
        desired_setup_script = setup_script.strip() if setup_script else None
        async with workspaces.provision(chat["id"]):
            record = await devboxes.create(chat["id"], title, desired_repos)
            yield "creating devbox (cold boot, about a minute)…"
            try:
                record["set_id"] = await devbox.create_taskset(
                    f"hatchery {chat['id']} / {record['title']}"
                )
                record["box"] = await devbox.create_box(
                    f"hatchery-{chat['id']}-{record['id'][-6:]}",
                    desired_repos,
                    desired_setup_script,
                )
                record["setup_script"] = desired_setup_script
                record["state"] = "ready"
            except Exception as error:
                record["state"] = "errored"
                record["error"] = str(error)
                await devboxes.save(record)
                await events.append(chat["id"], "ui", {"type": "devbox.changed"})
                raise
            await devboxes.save(record)
            await events.append(chat["id"], "ui", {"type": "devbox.changed"})
        yield {
            "devbox_id": record["id"],
            "title": record["title"],
            "repos": record["repos"],
            "state": record["state"],
        }

    @ai.tool
    async def list_devboxes() -> list[dict[str, typing.Any]]:
        """List this chat's reusable devboxes, oldest first."""
        return [
            {
                key: record.get(key)
                for key in ("id", "title", "repos", "state", "error", "created_at")
            }
            for record in await devboxes.list_for_chat(chat["id"])
        ]

    @ai.tool
    async def create_subagent(
        devbox_id: str,
        task: str,
        model: str = devbox.DEFAULT_MODEL,
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Start a coding subagent inside one of this chat's devboxes.

        The task should be self-contained and say what done looks like. Multiple
        subagents may share a devbox, but avoid concurrent edits unless intended.
        """
        workspace = await devboxes.get(devbox_id)
        if workspace is None or workspace.get("chat_id") != chat["id"]:
            raise ValueError("devbox does not belong to this chat")
        if workspace.get("state") != "ready" or not workspace.get("box"):
            raise RuntimeError("devbox is not ready")

        yield "dispatching subagent…"
        launch = await subagents.create(
            chat["id"], workspace["id"], task, secrets.token_urlsafe(32)
        )
        launch["model"] = model
        await subagents.save(launch)
        await chats.finish(chat["id"], "running")
        await events.append(chat["id"], "ui", {"type": "chat.changed"})
        try:
            created = await devbox.create_task(
                workspace["box"]["id"],
                workspace["set_id"],
                task,
                launch["webhook_secret"],
                launch["id"],
                model,
            )
        except Exception as error:
            launch["state"] = "errored"
            launch["result"] = {"error": str(error)}
            await subagents.save(launch)
            await activity.append(
                launch["id"],
                "state_transition",
                {"from": "creating", "to": "errored", "error": str(error)},
            )
            siblings = await subagents.list_for_chat(chat["id"])
            if not any(
                sibling["id"] != launch["id"]
                and sibling.get("state") not in devbox.TERMINAL_STATES
                for sibling in siblings
            ):
                await chats.finish(chat["id"], "failed", str(error))
            await events.append(
                chat["id"],
                "ui",
                {
                    "type": "task.changed",
                    "launch_id": launch["id"],
                    "devbox_id": workspace["id"],
                    "state": "errored",
                },
            )
            raise
        launch = await subagents.finish_create(launch["id"], created)
        await events.append(
            chat["id"],
            "ui",
            {
                "type": "task.changed",
                "launch_id": launch["id"],
                "devbox_id": workspace["id"],
                "state": launch["state"],
            },
        )

        if on_task_created is not None:
            on_task_created(dict(launch), created)
        if devbox.webhook_url() is not None:
            run = await workflow.start(supervisor.supervise, launch["id"])
            launch["supervision_run_id"] = run.run_id
            await subagents.save(launch)

        yield {
            "subagent_id": launch["id"],
            "devbox_id": workspace["id"],
            "task_id": created["task_id"],
            "state": created["state"],
        }

    @ai.tool
    async def check_subagent(
        subagent_id: str | None = None,
        after: int | None = None,
        limit: int = 20,
    ) -> dict[str, typing.Any]:
        """Check a subagent's state and recent activity.

        Omit subagent_id to inspect this chat's newest subagent. Pass cursor as
        after on a later check to receive only newer activity.
        """
        return await activity.status(chat["id"], subagent_id, after=after, limit=limit)

    return ai.Agent(
        tools=[create_devbox, list_devboxes, create_subagent, check_subagent]
    )
