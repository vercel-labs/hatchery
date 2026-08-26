import pytest

from agent import dispatcher
from store import activity, chats, events, spaces, tasks


async def test_check_coder_reads_chat_activity():
    launch = await tasks.create("chat_1", "fix it", "secret")
    launch["task_id"] = "task_1"
    launch["state"] = "running"
    await tasks.save(launch)
    await activity.append(
        launch["id"],
        "assistant_event",
        {"name": "assistant_message", "body": {"text": "Found the bug"}},
    )

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "check_coder")
    result = await tool.fn()

    assert result["launch_id"] == launch["id"]
    assert result["state"] == "running"
    assert result["events"] == [
        {"cursor": 0, "kind": "assistant_event", "summary": "Found the bug"}
    ]


async def test_launch_coder_clones_space_repos_and_persists_workspace(monkeypatch):
    space = await spaces.default()
    chat = await chats.create(space.id, "inspect")
    seen = {}

    async def create_taskset(title):
        return "set_1"

    async def create_box(name, repos):
        seen["box"] = (name, repos)
        return {"id": "box_1", "url": "https://box.example"}

    async def create_task(box_id, set_id, prompt, webhook_secret, webhook_task_id):
        seen["task"] = (box_id, set_id, prompt)
        return {"task_id": "task_1", "session_id": "session_1", "state": "pending"}

    monkeypatch.setattr(dispatcher.devbox, "create_taskset", create_taskset)
    monkeypatch.setattr(dispatcher.devbox, "create_box", create_box)
    monkeypatch.setattr(dispatcher.devbox, "create_task", create_task)
    monkeypatch.setattr(dispatcher.devbox, "webhook_url", lambda: None)

    agent = dispatcher.agent_for({"id": chat.id}, space.repos)
    tool = next(tool for tool in agent.tools if tool.name == "launch_coder")
    updates = [update async for update in tool.fn("answer questions")]

    assert seen["box"] == (f"hatchery-{chat.id}", ["vercel/vercel-py"])
    assert seen["task"] == ("box_1", "set_1", "answer questions")
    assert updates[-1]["task_id"] == "task_1"
    workspace = await events.tail(chat.id, "worker")
    assert workspace is not None
    assert workspace["repos"] == ["vercel/vercel-py"]
    assert workspace["workspace_version"] == 1
    saved_chat = await chats.get(chat.id)
    assert saved_chat is not None and saved_chat.status == "running"


async def test_launch_coder_replaces_repo_less_workspace(monkeypatch):
    space = await spaces.default()
    chat = await chats.create(space.id, "inspect")
    await events.append(
        chat.id,
        "worker",
        {"id": chat.id, "set_id": "set_1", "box": {"id": "old", "url": "https://old"}},
    )
    created_boxes = []

    async def create_box(name, repos):
        created_boxes.append(repos)
        return {"id": "new", "url": "https://new"}

    async def create_task(*args):
        return {"task_id": "task_1", "session_id": "session_1", "state": "pending"}

    monkeypatch.setattr(dispatcher.devbox, "create_box", create_box)
    monkeypatch.setattr(dispatcher.devbox, "create_task", create_task)
    monkeypatch.setattr(dispatcher.devbox, "webhook_url", lambda: None)

    agent = dispatcher.agent_for({"id": chat.id}, space.repos)
    tool = next(tool for tool in agent.tools if tool.name == "launch_coder")
    [update async for update in tool.fn("inspect")]

    assert created_boxes == [["vercel/vercel-py"]]
    workspace = await events.tail(chat.id, "worker")
    assert workspace is not None and workspace["box"]["id"] == "new"


async def test_launch_coder_rejects_concurrent_task(monkeypatch):
    space = await spaces.default()
    chat = await chats.create(space.id, "inspect")
    active = await tasks.create(chat.id, "first", "secret")
    active["state"] = "running"
    await tasks.save(active)

    agent = dispatcher.agent_for({"id": chat.id}, space.repos)
    tool = next(tool for tool in agent.tools if tool.name == "launch_coder")
    with pytest.raises(RuntimeError, match="already running"):
        [update async for update in tool.fn("second")]


async def test_provision_failure_marks_chat_failed_and_keeps_taskset(monkeypatch):
    space = await spaces.default()
    chat = await chats.create(space.id, "inspect")

    async def create_taskset(title):
        return "set_1"

    async def create_box(name, repos):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(dispatcher.devbox, "create_taskset", create_taskset)
    monkeypatch.setattr(dispatcher.devbox, "create_box", create_box)

    agent = dispatcher.agent_for({"id": chat.id}, space.repos)
    tool = next(tool for tool in agent.tools if tool.name == "launch_coder")
    with pytest.raises(RuntimeError, match="clone failed"):
        [update async for update in tool.fn("inspect")]

    assert await tasks.list_for_chat(chat.id) == []
    workspace = await events.tail(chat.id, "worker")
    assert workspace is not None and workspace["set_id"] == "set_1"
    saved_chat = await chats.get(chat.id)
    assert saved_chat is not None
    assert saved_chat.status == "failed"
    assert saved_chat.artifact == "clone failed"


async def test_launch_failure_is_recorded(monkeypatch):
    space = await spaces.default()
    chat = await chats.create(space.id, "inspect")

    async def create_taskset(title):
        return "set_1"

    async def create_box(name, repos):
        return {"id": "box_1", "url": "https://box.example"}

    async def create_task(*args):
        raise RuntimeError("task API unavailable")

    monkeypatch.setattr(dispatcher.devbox, "create_taskset", create_taskset)
    monkeypatch.setattr(dispatcher.devbox, "create_box", create_box)
    monkeypatch.setattr(dispatcher.devbox, "create_task", create_task)

    agent = dispatcher.agent_for({"id": chat.id}, space.repos)
    tool = next(tool for tool in agent.tools if tool.name == "launch_coder")
    with pytest.raises(RuntimeError, match="task API unavailable"):
        [update async for update in tool.fn("inspect")]

    [launch] = await tasks.list_for_chat(chat.id)
    assert launch["state"] == "errored"
    assert launch["result"] == {"error": "task API unavailable"}
    saved_chat = await chats.get(chat.id)
    assert saved_chat is not None
    assert saved_chat.status == "failed"
    assert saved_chat.artifact == "task API unavailable"
