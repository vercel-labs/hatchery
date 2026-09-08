import ast
import inspect
import textwrap

import ai
import pytest

from agent import durable
from store import chats, events
import worker


def test_workflow_body_does_not_import_side_effect_modules():
    tree = ast.parse(textwrap.dedent(inspect.getsource(durable.run_turn.func)))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not imported


async def test_durable_tools_keep_effects_non_retriable():
    assert durable.llm_step.max_retries > 0
    assert durable.list_sandboxes_step.max_retries > 0
    assert durable.check_subagent_step.max_retries > 0
    assert durable.create_sandbox_step.max_retries == 0
    assert durable.create_subagent_step.max_retries == 0
    assert durable.message_subagent_step.max_retries == 0
    assert durable.require_attention_step.max_retries == 0
    assert durable.deliver_replies.max_retries == 0
    assert durable.find_channels_step.max_retries > 0
    assert durable.find_people_step.max_retries > 0
    assert durable.send_message_step.max_retries == 0


async def test_custom_loop_uses_context_and_workflow_stream(monkeypatch):
    calls = []

    async def model_step(context, writer):
        calls.append((context, writer))
        return ai.assistant_message("done")

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(durable, "llm_step", model_step)
    writer = Writer()
    agent = durable.DurableDispatcher("chat_1", writer)
    history = [ai.user_message("help")]
    token = durable.current_agent.set(agent)
    try:
        async with agent.run(ai.get_model("openai/test"), history) as result:
            async for _ in result:
                pass
    finally:
        durable.current_agent.reset(token)

    assert result.messages[-1].text == "done"
    assert len(calls) == 1
    assert calls[0][0].messages[0].role == "user"
    assert calls[0][1] is writer


async def test_tools_read_trusted_chat_id_from_current_agent(monkeypatch):
    calls = []

    async def step(chat_id):
        calls.append(chat_id)
        return []

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(durable, "list_sandboxes_step", step)
    agent = durable.DurableDispatcher("chat_1", Writer())
    token = durable.current_agent.set(agent)
    try:
        assert await durable.list_sandboxes.fn() == []
    finally:
        durable.current_agent.reset(token)

    assert calls == ["chat_1"]
    assert durable.list_sandboxes.tool.spec.params["properties"] == {}


@pytest.mark.parametrize("provider", ["slack", "github"])
@pytest.mark.parametrize("people", [None, ["person_1"]])
async def test_destination_tools_forward_trusted_scope_through_steps(monkeypatch, provider, people):
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
    for name in ("find_channels_step", "find_people_step", "send_message_step"):
        monkeypatch.setattr(durable, name, getattr(durable, name).func)

    class Writer:
        async def write(self, value):
            pass

    agent = durable.DurableDispatcher("chat_trusted", Writer(), turn_id="turn_stable")
    tools = {tool.name: tool for tool in agent.tools}
    token = durable.current_agent.set(agent)
    try:
        assert await tools["find_channels"].fn(provider, "release") == candidates
        assert await tools["find_people"].fn("Alex") == linked_people
        kwargs = {} if people is None else {"people": people}
        assert await tools["send_message"].fn(
            provider, "exact_destination", "done", **kwargs,
        ) == {"status": "sent"}
    finally:
        durable.current_agent.reset(token)

    assert calls == [
        ("chat_trusted", provider, "release"),
        ("chat_trusted", "Alex"),
        ("chat_trusted", provider, "exact_destination", "done", people, "turn_stable"),
    ]
    for name, fields in (
        ("find_channels", {"provider", "query"}),
        ("find_people", {"query"}),
        ("send_message", {"provider", "destination", "text", "people"}),
    ):
        properties = tools[name].tool.spec.params["properties"]
        assert set(properties) == fields
        if "provider" in fields:
            assert properties["provider"]["enum"] == ["slack", "github"]


async def test_durable_require_attention_uses_trusted_chat_id(monkeypatch):
    calls = []

    async def step(chat_id, reason):
        calls.append((chat_id, reason))
        return {"reason": reason}

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(durable, "require_attention_step", step)
    agent = durable.DurableDispatcher("chat_trusted", Writer())
    token = durable.current_agent.set(agent)
    try:
        assert await durable.require_attention.fn("result_available") == {
            "reason": "result_available"
        }
    finally:
        durable.current_agent.reset(token)

    assert calls == [("chat_trusted", "result_available")]
    properties = durable.require_attention.tool.spec.params["properties"]
    assert set(properties) == {"reason"}
    assert properties["reason"]["enum"] == ["result_available", "blocked"]


async def test_create_sandbox_tool_forwards_size(monkeypatch):
    calls = []

    async def step(*args):
        calls.append(args)
        return {"id": "wrk_1"}

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(durable, "create_sandbox_step", step)
    agent = durable.DurableDispatcher("chat_1", Writer())
    token = durable.current_agent.set(agent)
    try:
        assert await durable.create_sandbox.fn(size="big") == {"id": "wrk_1"}
    finally:
        durable.current_agent.reset(token)

    assert calls[0][-1] == "big"


async def test_commit_messages_is_idempotent():
    message = ai.assistant_message("done")

    assert await durable.commit_messages.func("chat_1", [message]) == ["done"]
    assert await durable.commit_messages.func("chat_1", [message]) == ["done"]

    stored = await events.read("chat_1", "messages")
    assert len(stored) == 1
    assert ai.messages.Message.model_validate(stored[0][1]).text == "done"


async def test_deliver_replies_finishes_worker_completion(monkeypatch):
    chat = await chats.create("spc_1", "task")
    task = worker.Task(
        id="task_1",
        chat_id=chat.id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        status="complete",
        event_sequence=2,
        completion_sequence=2,
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:00:00+00:00",
    )
    await worker.store.save_task(task)
    delivered = []

    async def deliver(chat_id, message, *, final=True):
        delivered.append((chat_id, message, final))
        return []

    from app import server

    monkeypatch.setattr(server, "_deliver", deliver)
    turn = durable.TurnInput(chat_id=chat.id, origin="worker", task_id=task.id)
    await durable.deliver_replies.func(turn, ["working", "done"])

    assert delivered == [
        (chat.id, "working", False),
        (chat.id, "done", True),
    ]
    current = await worker.get_task(chat.id, task.id)
    assert current.completion_message == "done"
    assert current.completion_delivered is True
    assert (await chats.get(chat.id)).status == "done"


async def test_active_turn_reconciles_failed_workflow(monkeypatch):
    await events.append(
        "chat_1",
        "turns",
        {
            "type": "turn.started",
            "turn_id": "turn_1",
            "run_id": "run_1",
            "origin": "ui",
            "task_id": None,
        },
    )

    class Run:
        async def status(self):
            return "failed"

    monkeypatch.setattr(durable.vercel.workflow, "Run", lambda _run_id: Run())

    assert await durable.active_turn("chat_1") is None
    assert (await events.read("chat_1", "turns"))[-1][1]["type"] == "turn.failed"


async def test_cron_register_turn_rejects_duplicate_run(monkeypatch):
    async def claim_run(_turn_id, run_id):
        return run_id == "run_1"

    async def started_run(_turn_id):
        return "run_1"

    from store import jobs

    monkeypatch.setattr(jobs, "claim_run", claim_run)
    monkeypatch.setattr(jobs, "started_run", started_run)
    turn = durable.TurnInput(chat_id="chat_1", turn_id="turn_stable", origin="cron")

    await durable.register_turn.func(turn, "run_1")
    with pytest.raises(RuntimeError, match="already owned"):
        await durable.register_turn.func(turn, "run_2")

    started = [
        data for _, data in await events.read("chat_1", "turns")
        if data.get("type") == "turn.started"
    ]
    assert [event["run_id"] for event in started] == ["run_1"]


async def test_start_turn_registers_before_announcing(monkeypatch):
    seen = {}

    class Run:
        run_id = "run_1"

    async def start(workflow, payload):
        seen["workflow"] = workflow
        seen["payload"] = payload
        return Run()

    monkeypatch.setattr(durable.vercel.workflow, "start", start)

    turn = await durable.start_turn("chat_1", "worker", "task_1")

    assert turn.run_id == "run_1"
    assert turn.turn_id.startswith("turn_")
    assert seen["workflow"] is durable.run_turn
    assert seen["payload"] == durable.TurnInput(
        chat_id="chat_1",
        turn_id=turn.turn_id,
        origin="worker",
        task_id="task_1",
    )
    assert await events.read("chat_1", "turns") == [
        (
            0,
            {
                "type": "turn.started",
                "turn_id": turn.turn_id,
                "run_id": "run_1",
                "origin": "worker",
                "task_id": "task_1",
            },
        )
    ]
    assert (await events.read("chat_1", "ui"))[-1][1] == {
        "type": "stream.available",
        "turn_id": turn.turn_id,
        "run_id": "run_1",
        "generation": 0,
    }
