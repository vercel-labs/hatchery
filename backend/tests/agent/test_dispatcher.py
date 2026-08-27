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

    async def create_box(name, repos, setup_script=None):
        seen["box"] = (name, repos, setup_script)
        return {"id": "box_1", "url": "https://box.example"}

    monkeypatch.setattr(dispatcher.devbox, "create_taskset", create_taskset)
    monkeypatch.setattr(dispatcher.devbox, "create_box", create_box)

    agent = dispatcher.agent_for({"id": chat.id})
    tool = next(tool for tool in agent.tools if tool.name == "create_devbox")
    updates = [
        update
        async for update in tool.fn(
            repos=["vercel/vercel-py"], setup_script="  uv sync  ", title="main workspace"
        )
    ]

    [workspace] = await devboxes.list_for_chat(chat.id)
    assert workspace["state"] == "ready"
    assert workspace["repos"] == ["vercel/vercel-py"]
    assert workspace["box"] == {"id": "box_1", "url": "https://box.example"}
    assert workspace["set_id"] == "set_1"
    assert updates[-1]["devbox_id"] == workspace["id"]
    assert seen["box"][1:] == (["vercel/vercel-py"], "uv sync")
    assert workspace["setup_script"] == "uv sync"


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


async def test_check_subagent_reads_chat_activity():
    launch = await subagents.create("chat_1", "devbox_1", "fix it", "secret")
    launch["state"] = "running"
    await subagents.save(launch)
    await activity.append(
        launch["id"],
        "assistant_event",
        {"name": "assistant_message", "body": {"text": "Found the bug"}},
    )

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "check_subagent")
    result = await tool.fn()

    assert result["subagent_id"] == launch["id"]
    assert result["state"] == "running"
    assert result["events"] == [
        {"cursor": 0, "kind": "assistant_event", "summary": "Found the bug"}
    ]
