import pytest

import models
from agent import dispatcher
from store import activity, chats, devboxes, spaces, subagents


def test_system_prompt_describes_space_and_reuse():
    space = models.Space(
        id="spc_docs",
        name="docs",
        about="Keep the SDK documentation accurate.",
        repos=["vercel/vercel-py"],
        resources=[],
        color="#38bdf8",
        created_at="2026-08-26T00:00:00+00:00",
    )

    prompt = dispatcher.system_prompt(space)

    assert "Name: docs" in prompt
    assert "vercel/vercel-py" in prompt
    assert "Reuse an\nexisting devbox" in prompt


async def test_create_devbox_persists_chat_owned_box(monkeypatch):
    space = await spaces.default()
    chat = await chats.create(space.id, "inspect")
    seen = {}

    async def create_taskset(title):
        seen["taskset"] = title
        return "set_1"

    async def create_box(
        name, repos, setup_script=None, ports=None, branch=None, git_sha=None
    ):
        seen["box"] = (name, repos, setup_script, ports, branch, git_sha)
        return {"id": "box_1", "url": "https://box.example"}

    monkeypatch.setattr(dispatcher.devbox, "create_taskset", create_taskset)
    monkeypatch.setattr(dispatcher.devbox, "create_box", create_box)

    agent = dispatcher.agent_for({"id": chat.id})
    tool = next(tool for tool in agent.tools if tool.name == "create_devbox")
    updates = [
        update
        async for update in tool.fn(
            repos=["vercel/vercel-py"],
            setup_script="  uv sync  ",
            ports=[3000, 8000],
            branch="  feature/api  ",
            git_sha="  abc123  ",
            title="main workspace",
        )
    ]

    [workspace] = await devboxes.list_for_chat(chat.id)
    assert workspace["state"] == "ready"
    assert workspace["repos"] == ["vercel/vercel-py"]
    assert workspace["box"] == {"id": "box_1", "url": "https://box.example"}
    assert workspace["set_id"] == "set_1"
    assert updates[-1]["devbox_id"] == workspace["id"]
    assert seen["box"][1:] == (
        ["vercel/vercel-py"],
        "uv sync",
        [3000, 8000],
        "feature/api",
        "abc123",
    )
    assert workspace["setup_script"] == "uv sync"
    assert workspace["ports"] == [3000, 8000]
    assert workspace["branch"] == "feature/api"
    assert workspace["git_sha"] == "abc123"
    assert updates[-1]["ports"] == [3000, 8000]


async def test_list_devboxes_only_returns_current_chat():
    first = await devboxes.create("chat_1", "main", ["a/b"])
    first["state"] = "ready"
    await devboxes.save(first)
    await devboxes.create("chat_2", "other", [])

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "list_devboxes")

    assert await tool.fn() == [
        {
            "id": first["id"],
            "title": "main",
            "repos": ["a/b"],
            "ports": None,
            "branch": None,
            "git_sha": None,
            "state": "ready",
            "error": None,
            "created_at": first["created_at"],
        }
    ]


async def test_create_subagent_uses_selected_devbox(monkeypatch):
    workspace = await devboxes.create("chat_1", "main", ["a/b"])
    workspace.update(
        {
            "state": "ready",
            "set_id": "set_1",
            "box": {"id": "box_1", "url": "https://box.example"},
        }
    )
    await devboxes.save(workspace)
    seen = {}

    async def create_task(box_id, set_id, prompt, secret, launch_id, model):
        seen["task"] = (box_id, set_id, prompt, model)
        return {"task_id": "task_1", "session_id": "session_1", "state": "pending"}

    monkeypatch.setattr(dispatcher.devbox, "create_task", create_task)
    monkeypatch.setattr(dispatcher.devbox, "webhook_url", lambda: None)

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "create_subagent")
    updates = [
        update
        async for update in tool.fn(
            workspace["id"], "answer questions", "anthropic/claude-sonnet-4.6"
        )
    ]

    [launch] = await subagents.list_for_chat("chat_1")
    assert launch["devbox_id"] == workspace["id"]
    assert seen["task"] == (
        "box_1",
        "set_1",
        "answer questions",
        "anthropic/claude-sonnet-4.6",
    )
    assert updates[-1]["subagent_id"] == launch["id"]


async def test_create_subagent_rejects_another_chats_devbox():
    workspace = await devboxes.create("chat_2", "other", [])
    workspace.update(
        {
            "state": "ready",
            "set_id": "set_1",
            "box": {"id": "box_1", "url": "https://box.example"},
        }
    )
    await devboxes.save(workspace)

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "create_subagent")
    with pytest.raises(ValueError, match="does not belong"):
        [update async for update in tool.fn(workspace["id"], "inspect")]


async def test_multiple_subagents_can_share_devbox(monkeypatch):
    workspace = await devboxes.create("chat_1", "main", [])
    workspace.update(
        {
            "state": "ready",
            "set_id": "set_1",
            "box": {"id": "box_1", "url": "https://box.example"},
        }
    )
    await devboxes.save(workspace)
    created = 0

    async def create_task(*args):
        nonlocal created
        created += 1
        return {
            "task_id": f"task_{created}",
            "session_id": f"session_{created}",
            "state": "pending",
        }

    monkeypatch.setattr(dispatcher.devbox, "create_task", create_task)
    monkeypatch.setattr(dispatcher.devbox, "webhook_url", lambda: None)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "create_subagent")

    [update async for update in tool.fn(workspace["id"], "first")]
    [update async for update in tool.fn(workspace["id"], "second")]

    launches = await subagents.list_for_chat("chat_1")
    assert len(launches) == 2
    assert {launch["devbox_id"] for launch in launches} == {workspace["id"]}


async def test_message_subagent_resumes_existing_task(monkeypatch):
    older = await subagents.create("chat_1", "devbox_1", "older", "secret")
    older.update({"task_id": "task_1", "state": "running"})
    await subagents.save(older)
    launch = await subagents.create("chat_1", "devbox_1", "fix it", "secret")
    launch.update(
        {
            "task_id": "task_2",
            "state": "complete",
            "result": {"summary": "first result"},
            "completion_delivered": True,
            "completion_message": "done",
        }
    )
    await subagents.save(launch)
    seen = []

    async def send_task_prompt(task_id, prompt):
        seen.append((task_id, prompt))
        return {"task_id": task_id, "state": "complete"}

    monkeypatch.setattr(dispatcher.devbox, "send_task_prompt", send_task_prompt)
    monkeypatch.setattr(dispatcher.devbox, "webhook_url", lambda: None)

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "message_subagent")
    result = await tool.fn("use the existing helper")

    saved = await subagents.get(launch["id"])
    assert seen == [("task_2", "use the existing helper")]
    assert result == {
        "subagent_id": launch["id"],
        "task_id": "task_2",
        "state": "running",
    }
    assert saved is not None
    assert saved["state"] == "running"
    assert saved["completion_delivered"] is False
    assert "result" not in saved
    assert "completion_message" not in saved
    assert len(await subagents.list_for_chat("chat_1")) == 2


async def test_message_subagent_observer_marks_completed_resume(monkeypatch):
    launch = await subagents.create("chat_1", "devbox_1", "fix it", "secret")
    launch.update({"task_id": "task_1", "state": "complete"})
    await subagents.save(launch)
    observed = []

    async def send_task_prompt(task_id, prompt):
        return {"task_id": task_id, "state": "complete"}

    monkeypatch.setattr(dispatcher.devbox, "send_task_prompt", send_task_prompt)
    monkeypatch.setattr(dispatcher.devbox, "webhook_url", lambda: None)
    agent = dispatcher.agent_for(
        {"id": "chat_1"}, lambda record, sent: observed.append(sent)
    )
    tool = next(tool for tool in agent.tools if tool.name == "message_subagent")

    await tool.fn("continue")

    assert observed == [
        {
            "task_id": "task_1",
            "state": "complete",
            "resumed": True,
            "was_terminal": True,
        }
    ]


async def test_message_subagent_rejects_wrong_chat_and_errored_task(monkeypatch):
    foreign = await subagents.create("chat_2", "devbox_2", "foreign", "secret")
    foreign.update({"task_id": "task_foreign", "state": "running"})
    await subagents.save(foreign)
    failed = await subagents.create("chat_1", "devbox_1", "failed", "secret")
    failed.update({"task_id": "task_failed", "state": "errored"})
    await subagents.save(failed)

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "message_subagent")
    with pytest.raises(ValueError, match="does not belong"):
        await tool.fn("continue", foreign["id"])
    with pytest.raises(RuntimeError, match="errored"):
        await tool.fn("continue", failed["id"])
    with pytest.raises(ValueError, match="no subagent"):
        await tool.fn("continue")


async def test_check_subagent_reads_authoritative_state_and_activity(monkeypatch):
    launch = await subagents.create("chat_1", "devbox_1", "fix it", "secret")
    launch.update({"task_id": "task_1", "state": "pending"})
    await subagents.save(launch)
    await activity.append(
        launch["id"],
        "assistant_event",
        {"name": "assistant_message", "body": {"text": "Found the bug"}},
    )

    async def get_task(task_id):
        assert task_id == "task_1"
        return {
            "task_id": task_id,
            "state": "complete",
            "result": {"summary": "Fixed it"},
        }

    monkeypatch.setattr(dispatcher.devbox, "get_task", get_task)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "check_subagent")
    result = await tool.fn()

    assert result["subagent_id"] == launch["id"]
    assert result["state"] == "complete"
    assert result["result"] == {"summary": "Fixed it"}
    assert result["events"] == [
        {"cursor": 0, "kind": "assistant_event", "summary": "Found the bug"},
        {
            "cursor": 1,
            "kind": "state_transition",
            "summary": "state changed to complete",
        },
    ]


async def test_check_subagent_does_not_regress_terminal_state(monkeypatch):
    launch = await subagents.create("chat_1", "devbox_1", "fix it", "secret")
    launch.update(
        {"task_id": "task_1", "state": "complete", "result": {"summary": "done"}}
    )
    await subagents.save(launch)

    async def get_task(task_id):
        return {"task_id": task_id, "state": "running"}

    monkeypatch.setattr(dispatcher.devbox, "get_task", get_task)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "check_subagent")

    result = await tool.fn(launch["id"])

    assert result["state"] == "complete"
    assert result["result"] == {"summary": "done"}


async def test_check_subagent_waits_for_resumed_task_to_start(monkeypatch):
    launch = await subagents.create("chat_1", "devbox_1", "fix it", "secret")
    launch.update({"task_id": "task_1", "state": "running", "awaiting_resume": True})
    await subagents.save(launch)
    rows = iter(
        [
            {"task_id": "task_1", "state": "complete", "result": {"summary": "old"}},
            {"task_id": "task_1", "state": "running"},
        ]
    )

    async def get_task(task_id):
        return next(rows)

    monkeypatch.setattr(dispatcher.devbox, "get_task", get_task)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "check_subagent")

    assert (await tool.fn(launch["id"]))["state"] == "running"
    assert (await tool.fn(launch["id"]))["state"] == "running"
    saved = await subagents.get(launch["id"])
    assert saved is not None
    assert "awaiting_resume" not in saved
