"""The chat turn workflow: one durable run per user message.

Dispatch (channels.App or the UI api) appends the user message to the chat's
event stream and starts run_turn. The workflow reads its context back from
the store — project memory, repos, and the conversation derived from the
stream — so a crash resumes instead of losing the turn. Replies and progress
go out through emit_step: append to the stream (the UI tails it) and fan out
to every channel binding of the chat.

"parity" in the message hands off to the parity task (agent.tasks.parity)
instead of replying inline.
"""

import typing

import ai
import vercel.workflow

import agent
from channels import protocol
from store import chats, events, projects

MODEL_ID = "gateway:anthropic/claude-sonnet-4.6"
SYSTEM_PROMPT = """\
You are the software factory bot for the "{project}" project. You run mostly
unattended in the cloud; people reach you from slack threads, github issues,
and the factory UI — all feeding the same conversation.

Project repos: {repos}

Project memory (current state and direction, maintained by the team):
{memory}

Be terse. Plain sentences, no headers. A parity scan of the project repos can
be started by asking for "parity". If you don't know something, say so.
"""


def registry() -> dict[str, typing.Any]:
    """Channel adapters for delivery, built fresh per step call."""
    from channels import github, slack

    return {"slack": slack.channel(), "github": github.channel()}


@agent.workflow.step(max_retries=0)  # append + deliver is not idempotent
async def emit_step(chat_id: str, event_type: str, text: str) -> None:
    import channels

    if event_type == protocol.MESSAGE_COMPLETED:
        ev = channels.event(event_type, message=text)
    elif event_type == protocol.STATUS_UPDATED:
        ev = channels.event(event_type, status=text)
    elif event_type == protocol.TURN_FAILED:
        ev = channels.event(event_type, error=text)
    else:
        ev = channels.event(event_type)
    await channels.emit(registry(), chat_id, ev)


@agent.workflow.step
async def context_step(chat_id: str) -> dict:
    chat = await chats.get(chat_id)
    project = await projects.get(chat.project_id) if chat is not None else None
    records = await events.read(chat_id)
    history = protocol.history(protocol.Event.model_validate(data) for _, data in records)
    return {
        "project": project.name if project is not None else "",
        "memory": project.memory if project is not None else "",
        "repos": project.repos if project is not None else [],
        "history": [message.model_dump() for message in history],
    }


@agent.workflow.step
async def llm_step(messages_data: list[dict]) -> str:
    messages = [ai.messages.Message.model_validate(m) for m in messages_data]
    async with ai.stream(ai.get_model(MODEL_ID), messages) as stream:
        async for _event in stream:
            pass
    return stream.text


@agent.workflow.workflow
@ai.messages.use_random(vercel.workflow.random)
@ai.experimental_telemetry.use_time(vercel.workflow.time_ns)
async def run_turn(chat_id: str) -> str:
    await emit_step(chat_id, protocol.TURN_STARTED, "")
    try:
        context = await context_step(chat_id)
        last = next((m["content"] for m in reversed(context["history"]) if m["role"] == "user"), "")
        if "parity" in last.lower():
            from agent.tasks import parity  # late: parity imports this module

            await emit_step(chat_id, protocol.STATUS_UPDATED, "scanning repos...")
            await vercel.workflow.start(parity.parity_workflow, chat_id)
            return "parity run started"

        system = SYSTEM_PROMPT.format(
            project=context["project"] or "default",
            repos=", ".join(context["repos"]) or "none attached",
            memory=context["memory"] or "(empty)",
        )
        messages = [ai.system_message(system)] + [
            ai.user_message(m["content"]) if m["role"] == "user" else ai.assistant_message(m["content"])
            for m in context["history"]
        ]
        reply = await llm_step([message.model_dump(mode="json") for message in messages])
        await emit_step(chat_id, protocol.MESSAGE_COMPLETED, reply)
        await emit_step(chat_id, protocol.TURN_COMPLETED, "")
        return reply
    except Exception as error:
        await emit_step(chat_id, protocol.TURN_FAILED, f"{type(error).__name__}: {error}")
        raise
