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
