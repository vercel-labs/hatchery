"""Model tools: argument validation, bounded model previews, and Hatchery tool context.

Ported from agentmesh `tests/unit/test_agent_context.py` (tool cases).
"""

import json

import ai
import pytest
import rotor.testing

from hatchery import worker
from hatchery.agent import tools
from hatchery.store import chats

from tests.agent import conftest


def bash_call(**args: object) -> ai.messages.ToolCallPart:
    return ai.messages.ToolCallPart(
        tool_call_id="call-1", tool_name="bash", tool_args=json.dumps(args)
    )


def test_tool_result_keeps_full_output_but_gives_the_model_a_bounded_head_and_tail() -> None:
    value = {
        "exit_code": 9,
        "stdout": "start-" + "x" * 50_000 + "-stdout-end",
        "stderr": "error-" + "y" * 50_000 + "-stderr-end",
    }
    part = tools.result(bash_call(command="diagnose"), value)
    preview = part.get_model_input()
    assert part.result == value
    assert part.has_model_input and isinstance(preview, dict)
    encoded = json.dumps(preview, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= tools.MAX_TOOL_MODEL_BYTES
    assert "start-" in preview["stdout"] and "-stdout-end" in preview["stdout"]
    assert "bytes omitted" in preview["stdout"] and "bytes omitted" in preview["stderr"]


def test_bash_timeout_defaults_and_rejects_values_outside_the_tool_limit() -> None:
    assert tools.bash_timeout(bash_call(command="true"), 300) == 300
    assert tools.bash_timeout(bash_call(command="true", timeout_seconds=480), 300) == 480
    for invalid in (0, 481):
        with pytest.raises(ValueError, match="between 1 and 480"):
            tools.bash_timeout(bash_call(command="true", timeout_seconds=invalid), 300)
    assert tools.arguments(bash_call(command="ls", description="List files")) == {
        "command": "ls",
        "description": "List files",
        "timeout_seconds": None,
    }


def test_signal_delay_accepts_short_durations_only() -> None:
    assert tools.signal_delay({"delay": "90s"}) == 90
    assert tools.signal_delay({"delay": "2h"}) == 7200
    for invalid in ("0s", "soon", "5w"):
        with pytest.raises(ValueError, match="90s, 15m, 2h, or 1d"):
            tools.signal_delay({"delay": invalid})


def test_thread_tools_include_hatchery_tools_and_secret_requests() -> None:
    names = {tool.name for tool in tools.TOOLS}
    assert {"create_sandbox", "create_subagent", "start_thread", "bash", "delegate"} <= names
    assert "secret_request" in names


async def test_hatchery_tools_run_with_the_trusted_chat_context(monkeypatch) -> None:
    chat = await chats.create(conftest.AGENT, "tools")
    seen = []

    async def list_all(chat_id):
        seen.append(chat_id)
        return []

    monkeypatch.setattr("hatchery.agent.sandbox.list_all", list_all)
    call = ai.messages.ToolCallPart(
        tool_call_id="call_1", tool_name="list_sandboxes", tool_args="{}"
    )
    async with rotor.testing.LocalRuntime(tools.run_hatchery_tool) as rt:
        handle = await rt.client.start(
            tools.run_hatchery_tool,
            input={
                "chat_id": chat.id,
                "turn_id": "turn_1",
                "actor_user_id": "user_actor",
                "call": call.model_dump(mode="json"),
            },
        )
        await rt.drain()
        snapshot = await handle.snapshot()
    assert ai.messages.ToolResultPart.model_validate(snapshot.output).result == []
    assert seen == [chat.id]
    assert tools.list_sandboxes.tool.spec.params["properties"] == {}


async def test_open_terminals_and_active_fx_tasks_keep_a_sandbox_busy() -> None:
    chat = await chats.create(conftest.AGENT, "busy")
    name = "hatchery-" + "a" * 40
    assert await tools.busy(chat.id, name) is False
    await worker.store.save_terminal(
        worker.models.Terminal(
            id="term_1",
            chat_id=chat.id,
            worker_id="a" * 40,
            title="shell",
            status="running",
            created_at="2026-09-03T00:00:00+00:00",
            updated_at="2026-09-03T00:00:00+00:00",
        )
    )
    assert await tools.busy(chat.id, name) is True
    assert await tools.busy(chat.id, "hatchery-" + "b" * 40) is False
