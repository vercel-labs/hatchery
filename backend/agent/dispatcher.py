"""The dispatcher: the durable-side agent the user talks to.

It never codes inline. Its one tool hands work to the chat's devbox and
streams the task's state changes back (pushed over the watch websocket, no
polling), so the whole coding session happens inside a single tool call the
UI renders live — with the real claude code TUI attached in the terminal
pane next to it.
"""

import asyncio
import time

import ai

from agent import devbox
from store import events

SYSTEM = """\
You are fabricator's dispatcher. You coordinate coding work; you never write
code yourself. When the user wants something built, investigated, or fixed,
compose a clear self-contained task and call launch_coder. While it runs the
user watches the coder's terminal live in the next pane, so don't narrate
its steps. When it finishes, relay the result briefly: what was made, where
it is, anything that needs the user's attention. If the coder fails, say so
plainly and stop — never write the code yourself or invent what the output
would have looked like. Be terse and concrete."""

WATCH_TIMEOUT = 20 * 60


def model() -> ai.Model:
    return ai.get_model("anthropic/claude-sonnet-4.6")


def agent_for(chat: dict) -> ai.Agent:
    """Build the dispatcher agent bound to one chat's worker state.

    `chat` is the tail of the chat's (chat_id, "worker") stream; the tool
    writes the box / set / session ids on it and snapshots after each change,
    so the tty proxy and later turns find them across restarts.
    """

    @ai.tool
    async def launch_coder(task: str) -> ai.StreamingStatusTool[str]:
        """Hand a coding task to this chat's devbox and wait for the result.

        The task should be self-contained: what to build or do, and what
        "done" looks like. The coder is a real claude code session; the user
        observes it live and can type into its terminal while it runs.
        """
        if not chat.get("set_id") or not chat.get("box"):
            if not chat.get("set_id"):
                chat["set_id"] = await devbox.create_taskset(f"fab {chat['id']}")
            if not chat.get("box"):
                yield "creating devbox (cold boot, about a minute)…"
                chat["box"] = await devbox.create_box(f"fab-{chat['id']}")
            await events.append(chat["id"], "worker", dict(chat))

        # a fresh box reports READY before devboxd finishes installing the
        # assistants, so the first task can race the claude install and error
        # with "executable file not found". that settles in ~half a minute:
        # retry the task on the same box a few times before giving up.
        for attempt in (1, 2, 3):
            yield "dispatching task…"
            created = await devbox.create_task(chat["box"]["id"], chat["set_id"], task)
            chat["task_id"] = created["task_id"]
            chat["session_id"] = created["session_id"]
            await events.append(chat["id"], "worker", dict(chat))

            state = created["state"]
            summary = ""  # the coder's own completion summary, pushed on the stream
            yield f"coder started — terminal attached [{state}]"
            last_yield = time.monotonic()
            try:
                async with asyncio.timeout(WATCH_TIMEOUT):
                    async for frame in devbox.watch(chat["box"]["url"], created["task_id"]):
                        body = (frame or {}).get("body") or {}
                        if (event := body.get("assistantEvent")) and event.get("name") == "complete":
                            summary = (event.get("body") or {}).get("summary") or summary
                        transition = body.get("stateTransition")
                        if not transition:
                            # assistant events (or watch quiet) — the sse goes
                            # silent for minutes while the coder works, and
                            # idle-timeout proxies sever it. keep it warm.
                            if time.monotonic() - last_yield > 45:
                                yield f"[{state}] coder is working…"
                                last_yield = time.monotonic()
                            continue
                        state = transition["to"]
                        if state in devbox.TERMINAL_STATES:
                            break
                        yield f"[{state}]" + (
                            " — coder needs input, check the terminal"
                            if state == "attention-required"
                            else ""
                        )
                        last_yield = time.monotonic()
            except TimeoutError:
                pass

            # the durable row syncs behind the box (its state PATCH can land
            # seconds after the watch's terminal frame), so the pushed state
            # and summary above are the truth; the row only enriches — error
            # reason, pr urls — and never regresses a terminal state we saw.
            row = await devbox.get_task(created["task_id"])
            result = row.get("result") or {}
            if row.get("state") in devbox.TERMINAL_STATES:
                state = row["state"]
            if "executable file not found" in (result.get("error") or "") and attempt < 3:
                yield "devbox is still installing its tools — retrying in a moment…"
                await asyncio.sleep(20)
                continue
            parts = [f"[{state}]"]
            summary = summary or result.get("summary") or result.get("error")
            if summary:
                parts.append(summary)
            parts += [pr["url"] for pr in result.get("prs") or [] if pr.get("url")]
            yield " ".join(parts)
            return

    return ai.Agent(tools=[launch_coder])
