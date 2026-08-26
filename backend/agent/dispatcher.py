"""The dispatcher: the durable-side agent the user talks to.

It never codes inline. Its one tool hands work to the chat's devbox and
streams the task's state changes back (pushed over the watch websocket, no
polling), so the whole coding session happens inside a single tool call the
UI renders live — with the real claude code TUI attached in the terminal
pane next to it.
"""

import logging
import secrets
import typing

import ai
from vercel import workflow

from agent import access, devbox, supervisor
from store import activity, events, tasks, workspaces

log = logging.getLogger("app.dispatcher")

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

def model() -> ai.Model:
    return ai.get_model("anthropic/claude-sonnet-4.6")


def agent_for(
    chat: dict, on_task_created: typing.Callable[[dict, dict], None] | None = None
) -> ai.Agent:
    """Build the dispatcher agent bound to one chat's worker state.

    `chat` is the tail of the chat's (chat_id, "worker") stream. It owns the
    shared box and taskset; each launch stores its own task and PTY session.
    """

    @ai.tool
    async def launch_coder(task: str) -> ai.StreamingStatusTool[typing.Any]:
        """Hand a coding task to this chat's devbox.

        The task should be self-contained: what to build or do, and what
        "done" looks like. It returns as soon as the task is accepted;
        completion arrives in a later turn.
        """
        log.info(
            "coder launch starting chat_id=%s has_taskset=%s has_box=%s prompt_chars=%d",
            chat["id"],
            bool(chat.get("set_id")),
            bool(chat.get("box")),
            len(task),
        )
        auth = await access.for_chat(chat["id"])
        if not chat.get("set_id") or not chat.get("box"):
            async with workspaces.provision(chat["id"]):
                current = await events.tail(chat["id"], "worker") or chat
                chat.update(current)
                if not chat.get("set_id"):
                    chat["set_id"] = await devbox.create_taskset(
                        auth, f"hatchery {chat['id']}"
                    )
                if not chat.get("box"):
                    yield "creating devbox (cold boot, about a minute)…"
                    chat["box"] = await devbox.create_box(auth, f"hatchery-{chat['id']}")
                await events.append(chat["id"], "worker", dict(chat))
                log.info(
                    "coder workspace ready chat_id=%s box_id=%s set_id=%s",
                    chat["id"],
                    chat["box"]["id"],
                    chat["set_id"],
                )

        yield "dispatching task…"
        launch = await tasks.create(chat["id"], task, secrets.token_urlsafe(32))
        log.info("coder launch record created chat_id=%s launch_id=%s", chat["id"], launch["id"])
        created = await devbox.create_task(
            auth,
            chat["box"]["id"],
            chat["set_id"],
            task,
            launch["webhook_secret"],
            launch["id"],
        )
        launch = await tasks.finish_create(launch["id"], created)
        log.info(
            "coder launch accepted chat_id=%s launch_id=%s task_id=%s session_id=%s state=%s",
            chat["id"],
            launch["id"],
            created.get("task_id"),
            created.get("session_id"),
            created.get("state"),
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
