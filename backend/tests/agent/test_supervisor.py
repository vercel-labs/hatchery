"""The agent supervisor: routing, the delegation tree, limits, and child chats.

Ported from agentmesh `tests/integration/test_thread.py` (delegation scenarios).
"""

import typing

import ai
import ai.testing
import rotor

from hatchery import config, messages
from hatchery.agent import supervisor, thread, tools
from hatchery.store import chats, turns
from hatchery.workspace import git as workspace_git

from tests.agent import conftest

call = ai.testing.tool_call


def thread_ids(chat_id: str, *calls: ai.messages.ToolCallPart) -> tuple[list[str], list[str]]:
    """Task ids and thread ids down a chain of delegate calls from a chat's root."""
    parent = supervisor.process_id(conftest.AGENT)
    task_id = supervisor.root_key(chat_id)
    task_ids, ids = [task_id], [rotor.child_id(parent, task_id)]
    for delegated in calls:
        task_id = f"{task_id}:{delegated.tool_call_id}"
        task_ids.append(task_id)
        ids.append(rotor.child_id(parent, task_id))
    return task_ids, ids


def proposal_branch(thread_id: str, section: str = "workspace") -> str:
    digest = workspace_git.operation_digest(conftest.AGENT, thread_id, section, "proposal")
    return f"consolidations/{conftest.AGENT}/{section}/{digest}"


def parent_message(text: str) -> ai.messages.Message:
    return ai.user_message(f"{thread.PARENT_MESSAGE_HEADER}\nMessage:\n{text}")


def task_completion(
    handle: str,
    objective: str,
    summary: str,
    result: str,
    *,
    proposals: typing.Sequence[tuple[str, str]] = (),
    roster: typing.Sequence[tuple[str, str, str]] = (),
) -> str:
    lines = [
        "Delegated task completed:",
        f"Task: {handle}",
        f"Reply target: use `{handle}` with the task tools.",
        f"Objective: {objective}",
        f"Summary: {summary}",
        f"Result:\n{result}",
    ]
    if proposals:
        lines.append(
            "Proposals:\n" + "\n".join(f"- {section}: {branch}" for section, branch in proposals)
        )
    if roster:
        lines.append(
            "Child roster:\n"
            + "\n".join(f"- {task}: {status} \N{EM DASH} {title}" for task, status, title in roster)
        )
    return "\n".join(lines)


# Phase 4 gate


async def test_parent_child_grandchild_complete_with_parent_approval(
    run: conftest.Run, repo
) -> None:
    chat_id = (await chats.create(conftest.AGENT, "tree", user_id="user_test")).id
    to_child = call(tools.delegate, objective="Verify the fallback")
    to_grandchild = call(tools.delegate, objective="Check the edge case")
    task_ids, (root_id, child_id, grandchild_id) = thread_ids(chat_id, to_child, to_grandchild)
    root = [
        ai.user_message("Investigate the failing job"),
        ai.assistant_message(to_child, call(tools.idle, note="delegated verification")),
        ai.user_message(
            task_completion(
                "task-1",
                "Verify the fallback",
                "Fallback verified",
                "Fallback and edge case verified",
                proposals=[("workspace", proposal_branch(child_id))],
                roster=[("task-1", "completed", "Verify the fallback")],
            )
        ),
        ai.assistant_message(
            call(tools.review_proposal, task_id="task-1", section="workspace", decision="approve"),
            call(tools.idle, note="integrated the verification"),
        ),
    ]
    child = [
        parent_message("Verify the fallback"),
        ai.assistant_message(to_grandchild, call(tools.idle, note="delegated the edge case")),
        ai.user_message(
            task_completion(
                "task-1",
                "Check the edge case",
                "Edge case checked",
                "The edge case holds",
                proposals=[("workspace", proposal_branch(grandchild_id))],
                roster=[("task-1", "completed", "Check the edge case")],
            )
        ),
        ai.assistant_message(
            call(tools.review_proposal, task_id="task-1", section="workspace", decision="approve"),
            call(tools.bash, command="record-fallback"),
            call(
                tools.complete,
                summary="Fallback verified",
                result="Fallback and edge case verified",
            ),
        ),
    ]
    grandchild = [
        parent_message("Check the edge case"),
        ai.assistant_message(
            call(tools.bash, command="record-edge"),
            call(tools.complete, summary="Edge case checked", result="The edge case holds"),
        ),
    ]
    commands = {
        "record-edge": conftest.edit("self/memories/edge.md", "The edge case holds\n"),
        "record-fallback": conftest.edit("self/memories/fallback.md", "Fallback verified\n"),
    }
    async with run(root, child, grandchild, commands=commands) as app:
        await app.chat("Investigate the failing job", chat_id=chat_id)
        assert not app.model.unused

        roster = await app.roster()
        assert {t["thread_id"] for t in roster["threads"]} == {root_id, child_id, grandchild_id}
        assert all(t["status"] == "idle" for t in roster["threads"]), roster["threads"]
        child_task = roster["tasks"][task_ids[1]]
        grandchild_task = roster["tasks"][task_ids[2]]
        assert child_task["parent_thread_id"] == root_id and child_task["status"] == "completed"
        assert grandchild_task["parent_thread_id"] == child_id
        assert grandchild_task["status"] == "completed"
        assert grandchild_task["summary"] == "Edge case checked"
        assert [p["merged"] for p in child_task["proposals"]] == [True]
        assert [p["merged"] for p in grandchild_task["proposals"]] == [True]

        # Approved memory flowed grandchild -> child -> root -> main.
        _, main = await repo.read_main(conftest.AGENT)
        assert main["self/memories/edge.md"].content == b"The edge case holds\n"
        assert main["self/memories/fallback.md"].content == b"Fallback verified\n"
        root_details, _ = await app.rt.client.query(root_id, thread.AgentThread.details)
        assert root_details["summary"] == "integrated the verification"

        # Every thread has a chat; delegated chats hang off their parent's chat.
        child_chat = thread.child_chat_id(child_id)
        grandchild_chat = thread.child_chat_id(grandchild_id)
        assert [c.id for c in await chats.list_all(parent_chat_id=chat_id)] == [child_chat]
        assert [c.id for c in await chats.list_all(parent_chat_id=child_chat)] == [
            grandchild_chat
        ]
        assert chat_id in {c.id for c in await chats.list_all()}
        assert child_chat not in {c.id for c in await chats.list_all()}
        assert (await chats.get(child_chat)).trigger == "task"
        assert await supervisor.thread_for_chat(child_chat) == child_id
        for chat in (chat_id, child_chat, grandchild_chat):
            assert await turns.active(chat) is None
        from hatchery.app import server

        child_transcript = await server._transcript(child_chat)
        assert child_transcript[0].text == "Verify the fallback"
        assert child_transcript[0].provider_metadata["hatchery"]["source"] == "parent"


async def test_operator_prompt_in_a_child_chat_reaches_the_child_thread(
    run: conftest.Run,
) -> None:
    chat_id = (await chats.create(conftest.AGENT, "tree", user_id="user_test")).id
    to_child = call(tools.delegate, objective="Draft the summary")
    _, (_, child_id) = thread_ids(chat_id, to_child)
    root = [
        ai.user_message("Summarize the incident"),
        ai.assistant_message(to_child, call(tools.idle, note="delegated")),
        ai.user_message(
            "Message from delegated task:\nTask: task-1\n"
            "Reply target: use `task-1` with the task tools.\n"
            "Objective: Draft the summary\nMessage:\nDraft started"
        ),
        ai.assistant_message("The child started the draft."),
    ]
    child = [
        parent_message("Draft the summary"),
        ai.assistant_message(call(tools.message_parent, text="Draft started")),
        ai.assistant_message("Working on it."),
        ai.user_message("Use a bulleted list"),
        ai.assistant_message("Switched to bullets."),
    ]
    async with run(root, child) as app:
        await app.chat("Summarize the incident", chat_id=chat_id)
        child_chat = thread.child_chat_id(child_id)
        await app.chat("Use a bulleted list", chat_id=child_chat)
        details, _ = await app.rt.client.query(child_id, thread.AgentThread.details)
        assert details["status"] == "idle"
        assert details["result"] == "Switched to bullets."
        assert await turns.active(child_chat) is None
        assert not app.model.unused


async def test_delegation_depth_and_fanout_are_bounded(run: conftest.Run) -> None:
    settings = config.Config(
        thread=config.ThreadConfig(max_turns=4, max_delegations_per_thread=1),
        budget=config.BudgetConfig(tokens_per_day=1000),
    )
    first = call(tools.delegate, objective="first")
    second = call(tools.delegate, objective="second")
    root = [
        ai.user_message("Split the work"),
        ai.assistant_message(first, second, call(tools.idle, note="delegated")),
        ai.user_message(
            "Delegation rejected:\nTask: task-2\nObjective: second\n"
            "Reason: delegation fan-out limit (1) reached"
        ),
        ai.assistant_message("Only one child allowed."),
    ]
    child = [parent_message("first"), ai.assistant_message("On it.")]
    async with run(root, child, settings=settings) as app:
        await app.chat("Split the work")
        roster = await app.roster()
        assert len(roster["threads"]) == 2
        assert not app.model.unused


async def test_active_turn_repairs_a_turn_whose_thread_ended(
    run: conftest.Run, monkeypatch
) -> None:
    async with run([ai.user_message("hi"), ai.assistant_message("hello")]) as app:
        chat_id = await app.chat("hi")
        from hatchery.agent import thread as thread_module

        await thread_module.register_turn(
            messages.TurnInput(chat_id, "turn_stuck", "ui"), await app.thread_id(chat_id)
        )
        assert (await supervisor.active_turn(chat_id)).turn_id == "turn_stuck"
        await app.rt.client.send(
            supervisor.process_id(conftest.AGENT), messages.Retire()
        )
        await app.rt.drain()
        await app.rt.advance(f"{config.MAX_COMMAND_TIMEOUT_SECONDS + 5}s")
        assert await supervisor.active_turn(chat_id) is None
        assert (await turns.active(chat_id)) is None
