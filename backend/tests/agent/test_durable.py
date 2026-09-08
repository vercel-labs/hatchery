import ast
import inspect
import json
import textwrap
import types

import ai
import httpx
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


@pytest.mark.parametrize("tool_name", ["find_channels", "send_message"])
@pytest.mark.parametrize("reported", [False, True])
async def test_scope_failure_returns_from_step_but_raises_from_tool(monkeypatch, tool_name, reported):
    from channels import destinations

    monkeypatch.setenv("SLACK_CONNECTOR", "slack_test")
    body = {"needed": "groups:read,channels:read", "provided": "chat:write"} if reported else {}
    headers = httpx.Headers({"x-accepted-oauth-scopes": "channels:read"} if reported else {})
    method = "users.conversations" if tool_name == "find_channels" else "conversations.info"
    error = destinations.SlackScopeRequired(method, body, headers)

    async def service(*args, **kwargs):
        raise error

    monkeypatch.setattr(destinations, tool_name, service)
    step = getattr(durable, f"{tool_name}_step")
    args = ("slack", "release") if tool_name == "find_channels" else ("slack", "T1/C1", "done")
    step_args = args if tool_name == "find_channels" else (*args, None)
    kwargs = {} if tool_name == "find_channels" else {"delivery_key": "turn_1"}
    result = await step.func("chat_1", *step_args, **kwargs)

    assert result == {
        "status": "failed", "provider": "slack", "error": "missing_scope",
        "method": method, "connector": "slack_test",
        "needed": ["channels:read", "groups:read"] if reported else None,
        "provided": ["chat:write"] if reported else None,
        "accepted_scopes": ["channels:read"] if reported else None,
        "detail": str(error),
    }
    monkeypatch.setattr(durable, f"{tool_name}_step", step.func)
    agent = durable.DurableDispatcher("chat_1", None, turn_id="turn_1")
    token = durable.current_agent.set(agent)
    try:
        with pytest.raises(RuntimeError) as caught:
            await getattr(durable, tool_name).fn(*args)
    finally:
        durable.current_agent.reset(token)
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == result["detail"]


@pytest.mark.parametrize("tool_name,returned", [
    ("find_channels", False),
    ("send_message", False),
    ("send_message", True),
])
async def test_scope_failure_reaches_model_once_and_agent_continues(monkeypatch, tool_name, returned):
    from channels import destinations

    method = "users.conversations" if tool_name == "find_channels" else (
        "chat.postMessage" if returned else "conversations.info"
    )
    error = destinations.SlackScopeRequired(method, {"needed": "channels:read"}, httpx.Headers())
    attempts = []
    model_results = []
    args = {"provider": "slack", "query": "release"} if tool_name == "find_channels" else {
        "provider": "slack", "destination": "T1/C1", "text": "done",
    }

    async def service(*args, **kwargs):
        attempts.append((args, kwargs))
        if returned:
            return error.result
        raise error

    async def model_step(context, writer):
        if len(context.messages) == 1:
            return ai.assistant_message(ai.messages.ToolCallPart(
                tool_call_id="call_1", tool_name=tool_name, tool_args=json.dumps(args),
            ))
        model_results.extend(context.messages[-1].tool_results)
        return ai.assistant_message("Permissions need updating; I can continue with other work.")

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(destinations, tool_name, service)
    monkeypatch.setattr(durable, "llm_step", model_step)
    for name in (f"{tool_name}_step", "write_stream_event"):
        monkeypatch.setattr(durable, name, getattr(durable, name).func)
    agent = durable.DurableDispatcher("chat_1", Writer(), turn_id="turn_1")
    token = durable.current_agent.set(agent)
    try:
        async with agent.run(ai.get_model("openai/test"), [ai.user_message("notify release")]) as run:
            emitted = [event async for event in run]
    finally:
        durable.current_agent.reset(token)

    assert len(attempts) == 1
    assert len(model_results) == 1
    result = model_results[0]
    assert result.tool_call_id == "call_1"
    assert result.tool_name == tool_name
    assert result.is_error is True
    assert error.result["detail"] in result.get_model_input()
    tool_events = [event for event in emitted if isinstance(event, ai.events.ToolCallResult)]
    assert len(tool_events) == 1
    assert tool_events[0].message.tool_results == [result]
    assert run.messages[-1].text == "Permissions need updating; I can continue with other work."


async def test_find_channels_step_preserves_transient_exception(monkeypatch):
    from channels import destinations

    error = httpx.ReadTimeout("Slack temporarily unavailable")

    async def find_channels(*args):
        raise error

    monkeypatch.setattr(destinations, "find_channels", find_channels)
    assert durable.find_channels_step.max_retries > 0
    with pytest.raises(httpx.ReadTimeout) as caught:
        await durable.find_channels_step.func("chat_1", "slack", "release")
    assert caught.value is error


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


@pytest.mark.parametrize("shipping_fails", [False, True])
async def test_run_turn_ships_real_failed_spans_and_preserves_error(monkeypatch, shipping_fails):
    original = RuntimeError("agent loop failed")
    shipped = []
    live_spans = []
    channel_events = []

    class FailingAgent(ai.Agent):
        async def loop(self, context):
            yield ai.events.StreamEnd(message=ai.assistant_message("starting"))
            async with ai.experimental_telemetry.span("failed work") as span:
                live_spans.append(span)
                raise original

    class Writer:
        closed = False

        def __init__(self):
            self.events = []

        async def write(self, value):
            self.events.append(value)

        async def close(self):
            self.closed = True

    async def prepare(turn):
        return durable.PreparedTurn(history=[ai.user_message("help")])

    async def ship(spans):
        shipped.append(spans)
        if shipping_fails:
            raise RuntimeError("telemetry export failed")

    async def emit(*args):
        channel_events.append(args)

    writer = Writer()
    agent = FailingAgent()
    monkeypatch.setattr(durable, "DurableDispatcher", lambda *args: agent)
    monkeypatch.setattr(durable.vercel.workflow, "get_writable", lambda: writer)
    monkeypatch.setattr(durable.vercel.workflow, "get_workflow_metadata", lambda: types.SimpleNamespace(run_id="run_1"))
    monkeypatch.setattr(durable, "prepare_turn", prepare)
    monkeypatch.setattr(durable, "ship_spans", ship)
    monkeypatch.setattr(durable, "emit_turn_event", emit)
    for name in ("register_turn", "finish_turn", "write_lifecycle_event", "close_stream"):
        monkeypatch.setattr(durable, name, getattr(durable, name).func)
    turn = durable.TurnInput(chat_id="chat_1", turn_id="turn_1", origin="ui")
    previous_agent = durable.current_agent.get(None)

    # The real SDK task group wraps the loop error; export must not replace it.
    with pytest.raises(ExceptionGroup) as caught:
        await inspect.unwrap(durable.run_turn.func)(turn)

    assert caught.value.exceptions == (original,)
    assert durable.current_agent.get(None) is previous_agent
    assert len(shipped) == 1
    spans = shipped[0]
    failed_work = next(span for span in spans if span.name == "failed work")
    run_span = next(span for span in spans if span.data.kind == "run")
    assert failed_work.id == live_spans[0].id
    assert failed_work.parent_id == run_span.id
    for span, error in ((failed_work, original), (run_span, caught.value)):
        assert span.id
        assert span.started_at is not None
        assert span.ended_at >= span.started_at
        assert span.error.type == type(error).__name__
        assert span.error.message == str(error)
    assert (await events.read("chat_1", "turns"))[-1][1] == {
        "type": "turn.failed", "turn_id": "turn_1", "run_id": "run_1",
        "error": str(caught.value),
    }
    assert writer.events[-1]["type"] == "turn.failed"
    assert writer.events[-1]["error"] == str(caught.value)
    assert channel_events[-1] == ("chat_1", "turn.failed", str(caught.value))
    assert writer.closed is True


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
