"""The dispatcher: the durable-side agent the user talks to.

It never codes inline. Its one tool hands work to the chat's devbox and
streams the task's state changes back (pushed over the watch websocket, no
polling), so the whole coding session happens inside a single tool call the
UI renders live — with the real claude code TUI attached in the terminal
pane next to it.
"""

import secrets
import typing

import ai
from vercel import workflow

import models
from agent import devbox, supervisor
from store import activity, chats, events, tasks, workspaces

SYSTEM = """\
You are hatchery's dispatcher. You coordinate coding work; you never write
code yourself. When the user wants something built, investigated, or fixed,
compose a clear self-contained task and call launch_coder. While it runs the
user watches the coder's terminal live in the next pane, so don't narrate
its steps. A deployed launch only means the task was accepted: reply only that
work has started and stop. Use check_coder when the user asks about progress or
you are woken because coder state changed. Its result is authoritative. Never
launch another coder merely to check an existing one. If the coder fails, say
so plainly and stop — never write the code yourself or invent what the output
would have looked like. Be terse and concrete."""


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
    return ai.get_model("anthropic/claude-sonnet-4.6")


def agent_for(
    chat: dict,
    on_task_created: typing.Callable[[dict, dict], None] | None = None,
) -> ai.Agent:
    """Build the dispatcher agent bound to one chat's worker state.

    `chat` is the tail of the chat's (chat_id, "worker") stream. It owns the
    shared box and taskset; each launch stores its own task and PTY session.
    """

    @ai.tool
    async def launch_coder(
        task: str, repos: list[str] | None = None
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Hand a coding task to this chat's devbox.

        The task should be self-contained: what to build or do, and what
        "done" looks like. Select only the owner/repo repositories the coder
        needs. Omit repos for an empty sandbox. It returns as soon as the task
        is accepted; completion arrives in a later turn.
        """
        desired_repos = list(repos or [])
        async with workspaces.provision(chat["id"]):
            current = await events.tail(chat["id"], "worker") or chat
            chat.update(current)
            launches = await tasks.list_for_chat(chat["id"])
            if any(launch.get("state") not in devbox.TERMINAL_STATES for launch in launches):
                raise RuntimeError("a coder is already running for this chat")

            try:
                if not chat.get("set_id"):
                    chat["set_id"] = await devbox.create_taskset(f"hatchery {chat['id']}")
                    await events.append(chat["id"], "worker", dict(chat))
                if not chat.get("box") or chat.get("repos") != desired_repos:
                    yield "creating devbox (cold boot, about a minute)…"
                    chat["box"] = await devbox.create_box(
                        f"hatchery-{chat['id']}", desired_repos
                    )
                    chat["repos"] = desired_repos
                    chat["workspace_version"] = 1
                await events.append(chat["id"], "worker", dict(chat))
            except Exception as error:
                await chats.finish(chat["id"], "failed", str(error))
                raise

            yield "dispatching task…"
            launch = await tasks.create(chat["id"], task, secrets.token_urlsafe(32))
            await chats.finish(chat["id"], "running")
            try:
                created = await devbox.create_task(
                    chat["box"]["id"],
                    chat["set_id"],
                    task,
                    launch["webhook_secret"],
                    launch["id"],
                )
            except Exception as error:
                launch["state"] = "errored"
                launch["result"] = {"error": str(error)}
                await tasks.save(launch)
                await chats.finish(chat["id"], "failed", str(error))
                await activity.append(
                    launch["id"],
                    "state_transition",
                    {"from": "creating", "to": "errored", "error": str(error)},
                )
                raise
            launch = await tasks.finish_create(launch["id"], created)
            await events.append(
                chat["id"],
                "ui",
                {
                    "type": "task.changed",
                    "launch_id": launch["id"],
                    "state": launch["state"],
                },
            )

        if on_task_created is not None:
            on_task_created(dict(launch), created)
        if devbox.webhook_url() is not None:
            run = await workflow.start(supervisor.supervise, launch["id"])
            launch["supervision_run_id"] = run.run_id
            await tasks.save(launch)

        yield {
            "launch_id": launch["id"],
            "task_id": created["task_id"],
            "state": created["state"],
        }

    @ai.tool
    async def check_coder(
        launch_id: str | None = None,
        after: int | None = None,
        limit: int = 20,
    ) -> dict[str, typing.Any]:
        """Check a coder's current state and recent activity.

        Omit launch_id to inspect this chat's newest coder. Pass the returned
        cursor as after on a later check to receive only newer activity.
        """
        return await activity.status(chat["id"], launch_id, after=after, limit=limit)

    return ai.Agent(tools=[launch_coder, check_coder])
