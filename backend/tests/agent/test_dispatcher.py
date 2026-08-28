import pytest

import models
from agent import dispatcher


def test_system_prompt_marks_worker_layer_unavailable():
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

    assert "Vercel Sandbox worker layer" in prompt
    assert "not\nimplemented" in prompt
    assert "vercel/vercel-py" in prompt


async def test_worker_tools_are_explicit_stubs():
    agent = dispatcher.agent_for({"id": "chat_1"})
    tools = {tool.name: tool for tool in agent.tools}

    assert set(tools) == {
        "create_sandbox",
        "list_sandboxes",
        "create_subagent",
        "message_subagent",
        "check_subagent",
    }
    with pytest.raises(NotImplementedError, match="Sandbox control plane"):
        await tools["list_sandboxes"].fn()
