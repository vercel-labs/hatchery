import models
from agent import dispatcher


def space():
    return models.Space(
        id="spc_docs",
        name="docs",
        about="Keep the SDK documentation accurate.",
        repos=["vercel/vercel-py"],
        resources=[],
        color="#38bdf8",
        created_at="2026-08-26T00:00:00+00:00",
    )


def test_system_prompt_describes_worker_flow():
    prompt = dispatcher.system_prompt(space())
    assert "Sandboxes are durable" in prompt
    assert "create_subagent" in prompt
    assert "Choose small" in prompt
    assert "Choose big" in prompt
    assert "vercel/vercel-py" in prompt


async def test_worker_tools_are_chat_scoped(monkeypatch):
    seen = {}

    async def list_all(chat_id):
        seen["chat_id"] = chat_id
        return []

    monkeypatch.setattr(dispatcher.sandbox, "list_all", list_all)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tools = {tool.name: tool for tool in agent.tools}

    assert set(tools) == {
        "create_sandbox", "list_sandboxes", "create_subagent",
        "message_subagent", "check_subagent",
    }
    assert await tools["list_sandboxes"].fn() == []
    assert seen["chat_id"] == "chat_1"


async def test_create_sandbox_forwards_size(monkeypatch):
    seen = {}
    created = type("Worker", (), {"model_dump": lambda self, **_kwargs: {"id": "wrk_1"}})()

    async def create(chat_id, launch):
        seen.update(chat_id=chat_id, launch=launch)
        return created

    monkeypatch.setattr(dispatcher.sandbox, "create", create)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "create_sandbox")

    updates = [update async for update in tool.fn(size="big")]

    assert updates[-1] == {"id": "wrk_1"}
    assert seen["chat_id"] == "chat_1"
    assert seen["launch"].size == "big"


async def test_create_subagent_returns_task_id(monkeypatch):
    created = type("Task", (), {"id": "task_1", "worker_id": "wrk_1", "status": "pending"})()

    async def launch_task(chat_id, sandbox_id, task, model):
        assert (chat_id, sandbox_id, task, model) == (
            "chat_1", "wrk_1", "fix it", "openai/test",
        )
        return created

    monkeypatch.setattr(dispatcher.sandbox, "launch_task", launch_task)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "create_subagent")

    updates = [update async for update in tool.fn("wrk_1", "fix it", "openai/test")]

    assert updates[-1] == {
        "subagent_id": "task_1",
        "task_id": "task_1",
        "sandbox_id": "wrk_1",
        "state": "pending",
    }
