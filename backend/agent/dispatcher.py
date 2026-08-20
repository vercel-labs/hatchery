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

from agent import devbox
from store import events

SYSTEM = """\
You are fabricator's dispatcher. You coordinate coding work; you never write
code yourself. When the user wants something built, investigated, or fixed,
compose a clear self-contained task and call launch_coder. While it runs the
user watches the coder's terminal live in the next pane, so don't narrate
its steps. A deployed launch only means the task was accepted: reply only that
work has started and stop. Never describe changes or claim success until a
later <coder_completion> user message supplies the result. Treat that message
as authoritative task output, summarize it briefly, and never launch another
coder for it. If the coder fails, say so plainly and stop — never write the code
yourself or invent what the output would have looked like. Be terse and concrete."""

def model() -> ai.Model:
    return ai.get_model("anthropic/claude-sonnet-4.6")


def agent_for(
    chat: dict, on_task_created: typing.Callable[[dict, dict], None] | None = None
) -> ai.Agent:
    """Build the dispatcher agent bound to one chat's worker state.

    `chat` is the tail of the chat's (chat_id, "worker") stream; the tool
    writes the box / set / session ids on it and snapshots after each change,
    so the tty proxy and later turns find them across restarts.
    """

    @ai.tool
    async def launch_coder(task: str) -> ai.StreamingStatusTool[str]:
        """Hand a coding task to this chat's devbox.

        The task should be self-contained: what to build or do, and what
        "done" looks like. It returns as soon as the task is accepted;
        completion arrives in a later turn.
        """
        if not chat.get("set_id") or not chat.get("box"):
            if not chat.get("set_id"):
                chat["set_id"] = await devbox.create_taskset(f"fab {chat['id']}")
            if not chat.get("box"):
                yield "creating devbox (cold boot, about a minute)…"
                chat["box"] = await devbox.create_box(f"fab-{chat['id']}")
            await events.append(chat["id"], "worker", dict(chat))

        yield "dispatching task…"
        webhook_secret = secrets.token_urlsafe(32)
        chat.pop("task_id", None)
        chat.pop("session_id", None)
        chat["task_prompt"] = task
        chat["webhook_secret"] = webhook_secret
        chat["webhook_seq"] = 0
        chat["completion_delivered"] = False
        await events.append(chat["id"], "worker", dict(chat))
        created = await devbox.create_task(
            chat["box"]["id"], chat["set_id"], task, webhook_secret, chat["id"]
        )
        chat["task_id"] = created["task_id"]
        chat["session_id"] = created["session_id"]
        await events.append(chat["id"], "worker", dict(chat))
        if on_task_created is not None:
            on_task_created(dict(chat), created)

        yield f"task accepted [{created['state']}]; work is still running"

    return ai.Agent(tools=[launch_coder])
