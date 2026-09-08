import json
import uuid

import ai
import ai.testing
import httpx
import pytest

import models
from agent import dispatcher
from store import chats, events


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
    assert "require_attention with result_available" in prompt
    assert "blocked when work cannot continue" in prompt
    assert "Do not call it while routine follow-up work continues" in prompt
    assert "vercel/vercel-py" in prompt


def test_system_prompt_describes_notification_policy():
    prompt = dispatcher.system_prompt(space())
    for policy in (
        "space description and job prompt as prose",
        "explicit job-specific instructions override the space default",
        "search with find_channels and find_people",
        "select unambiguous actual candidates",
        "exact destination returned by find_channels",
        "Hatchery person IDs returned by find_people",
        "ask for clarification and call require_attention with blocked",
        "one-off sends: they create no bindings",
        "do not establish automatic two-way routing",
        "does not guarantee a notification",
        "If delivery is uncertain, do not retry",
        "when asked to notify or share and continue the conversation, use start_shared_thread",
        "If explicitly asked for a one-off, use send_message",
        "not earlier conversation text",
    ):
        assert policy in " ".join(prompt.split())


@pytest.mark.parametrize("provider", ["slack", "github"])
@pytest.mark.parametrize("people", [None, ["person_1"]])
async def test_destination_tools_are_chat_scoped(monkeypatch, provider, people):
    from channels import destinations

    calls = []
    candidates = [{"destination": "exact_destination"}]
    linked_people = [{"id": "person_1"}]

    async def find_channels(chat_id, provider, query):
        calls.append((chat_id, provider, query))
        return candidates

    async def find_people(chat_id, query):
        calls.append((chat_id, query))
        return linked_people

    async def send_message(chat_id, provider, destination, text, people, *, delivery_key):
        calls.append((chat_id, provider, destination, text, people, delivery_key))
        return {"status": "sent"}

    monkeypatch.setattr(destinations, "find_channels", find_channels)
    monkeypatch.setattr(destinations, "find_people", find_people)
    monkeypatch.setattr(destinations, "send_message", send_message)
    agent = dispatcher.agent_for({"id": "chat_trusted"})
    tools = {tool.name: tool for tool in agent.tools}

    assert await tools["find_channels"].fn(provider, "release") == candidates
    assert await tools["find_people"].fn("Alex") == linked_people
    kwargs = {} if people is None else {"people": people}
    for _ in range(2):
        assert await tools["send_message"].fn(
            provider, "exact_destination", "done", **kwargs,
        ) == {"status": "sent"}

    assert calls[:2] == [("chat_trusted", provider, "release"), ("chat_trusted", "Alex")]
    assert calls[2][:-1] == ("chat_trusted", provider, "exact_destination", "done", people)
    assert calls[2] == calls[3]
    assert uuid.UUID(calls[2][-1]).version == 4
    other = dispatcher.agent_for({"id": "chat_trusted"})
    send = next(tool for tool in other.tools if tool.name == "send_message")
    await send.fn(provider, "exact_destination", "done")
    assert calls[-1][-1] != calls[2][-1]

    for name, fields in (
        ("find_channels", {"provider", "query"}),
        ("find_people", {"query"}),
        ("send_message", {"provider", "destination", "text", "people"}),
        ("start_shared_thread", {"provider", "destination", "text", "people"}),
    ):
        properties = tools[name].tool.spec.params["properties"]
        assert set(properties) == fields
        if "provider" in fields:
            assert properties["provider"]["enum"] == ["slack", "github"]


@pytest.mark.parametrize("tool_name,returned", [
    ("find_channels", False),
    ("send_message", False),
    ("send_message", True),
])
async def test_destination_scope_failures_raise_tool_errors(monkeypatch, tool_name, returned):
    from channels import destinations

    method = "users.conversations" if tool_name == "find_channels" else (
        "chat.postMessage" if returned else "conversations.info"
    )
    error = destinations.SlackScopeRequired(method, {"needed": "channels:read"}, httpx.Headers())

    async def service(*args, **kwargs):
        if returned:
            return error.result
        raise error

    monkeypatch.setattr(destinations, tool_name, service)
    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == tool_name)
    args = ("slack", "release") if tool_name == "find_channels" else ("slack", "T1/C1", "done")
    with pytest.raises(RuntimeError) as caught:
        await tool.fn(*args)

    if returned:
        assert type(caught.value) is RuntimeError
        assert str(caught.value) == error.result["detail"]
    else:
        assert caught.value is error


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
        "message_subagent", "check_subagent", "require_attention",
        "find_channels", "find_people", "send_message", "start_shared_thread",
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


async def test_require_attention_uses_scoped_chat_and_validates_reason():
    chat = await chats.create(None, "work")
    agent = dispatcher.agent_for({"id": chat.id})
    tool = next(tool for tool in agent.tools if tool.name == "require_attention")

    assert "chat_id" not in tool.tool.spec.params["properties"]
    assert await tool.fn("blocked") == {"reason": "blocked"}
    assert (await chats.get(chat.id)).attention_reason == "blocked"
    assert await events.read(chat.id, "ui") == [(0, {"type": "chat.changed"})]

    with pytest.raises(Exception):
        await tool.tool.spec.params_adapter.validate_python({"reason": "routine"})


async def test_shared_thread_uses_live_context_and_persists_exact_anchor(monkeypatch):
    from app import server
    from channels import destinations

    history = [ai.user_message("share this conversation"), ai.assistant_message("private history"),
               ai.user_message("notify release and continue there")]
    before = ai.assistant_message("private planning", ai.messages.ToolCallPart(
        tool_call_id="actual_share_call", tool_name="start_shared_thread",
        tool_args=json.dumps({"provider": "slack", "destination": "T1/C1", "text": "Shared summary"}),
    ))
    after = ai.assistant_message("future public reply")
    model = ai.testing.FakeModel([*history, before, after])
    scopes = []
    mirrored = []

    async def share(chat_id, provider, destination, text, people, **scope):
        stored = await server._transcript(chat_id)
        assert stored[-1].tool_calls[0].tool_call_id == "actual_share_call"
        scopes.append(scope)
        return {"status": "sent", "sharing": {"id": "sharing_1"}}

    async def mirror(chat_id, sharing_id):
        mirrored.append((chat_id, sharing_id))

    monkeypatch.setattr(destinations, "start_shared_thread", share)
    monkeypatch.setattr(server, "_mirror_sharing", mirror, raising=False)
    agent = dispatcher.agent_for({"id": "chat_1"})
    async with agent.run(model, history) as run:
        emitted = [event async for event in run]

    actual_before = run.messages[len(history)]
    actual_after = run.messages[-1]
    assert scopes[0]["tool_call_id"] == "actual_share_call"
    assert scopes[0]["excluded_message_ids"] == [message.id for message in [*history, actual_before]]
    assert actual_after.id not in scopes[0]["excluded_message_ids"]
    assert mirrored == [("chat_1", "sharing_1")]
    results = [event for event in emitted if isinstance(event, ai.events.ToolCallResult)]
    assert len(results) == 1
    assert results[0].message.tool_results[0].tool_call_id == "actual_share_call"
    assert [message.id for message in await server._transcript("chat_1")] == [
        message.id for message in run.messages[len(history):]
    ]
