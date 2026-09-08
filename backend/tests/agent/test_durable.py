import ast
import contextlib
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
    assert durable.start_shared_thread_step.max_retries == 0
    assert durable.mirror_sharing.max_retries > 0


async def test_custom_loop_uses_context_and_workflow_stream(monkeypatch):
    calls = []

    async def model_step(context, writer):
        calls.append((context, writer))
        return ai.assistant_message("done")

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(durable, "llm_step", model_step)
    monkeypatch.setattr(durable, "commit_messages", durable.commit_messages.func)
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
        ("start_shared_thread", {"provider", "destination", "text", "people"}),
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
    ("start_shared_thread", False),
    ("start_shared_thread", True),
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
    for name in (f"{tool_name}_step", "write_stream_event", "commit_messages"):
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

    expected = [durable.Reply(id=message.id, text="done")]
    assert await durable.commit_messages.func("chat_1", [message]) == expected
    assert await durable.commit_messages.func("chat_1", [message]) == expected

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

    async def deliver(chat_id, message, *, final=True, message_id):
        delivered.append((chat_id, message, final, message_id))
        return []

    from app import server

    monkeypatch.setattr(server, "_deliver", deliver)
    turn = durable.TurnInput(chat_id=chat.id, origin="worker", task_id=task.id)
    await durable.deliver_replies.func(turn, [
        durable.Reply(id="reply_1", text="working"),
        durable.Reply(id="reply_2", text="done"),
    ])

    assert delivered == [
        (chat.id, "working", True, "reply_1"),
        (chat.id, "done", True, "reply_2"),
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

    async def drain(chat_id):
        from store import turns

        assert chat_id == "chat_1"
        assert await turns.active(chat_id) is None
        assert writer.closed
        assert writer.events[-1]["type"] == "turn.failed"
        channel_events.append((chat_id, "drained"))

    writer = Writer()
    agent = FailingAgent()
    monkeypatch.setattr(durable, "DurableDispatcher", lambda *args: agent)
    monkeypatch.setattr(durable.vercel.workflow, "get_writable", lambda: writer)
    monkeypatch.setattr(durable.vercel.workflow, "get_workflow_metadata", lambda: types.SimpleNamespace(run_id="run_1"))
    monkeypatch.setattr(durable, "prepare_turn", prepare)
    monkeypatch.setattr(durable, "ship_spans", ship)
    monkeypatch.setattr(durable, "emit_turn_event", emit)
    monkeypatch.setattr(durable, "drain_inbound", drain)
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
    assert channel_events[-2:] == [
        ("chat_1", "turn.failed", str(caught.value)), ("chat_1", "drained"),
    ]
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


async def test_prepare_turn_records_consumed_inbox_once():
    from store import spaces

    space = await spaces.create("work")
    chat = await chats.create(space.id, "work")
    first = ai.user_message("first inbound")
    await events.append(chat.id, "messages", first.model_dump(mode="json"))
    await events.append(chat.id, "inbox", {"message_id": first.id})
    turn = durable.TurnInput(chat_id=chat.id, turn_id="turn_1", origin="channel")
    prepared = await durable.prepare_turn.func(turn)
    assert prepared.history[-1] == first

    second = ai.user_message("arrived while running")
    await events.append(chat.id, "messages", second.model_dump(mode="json"))
    await events.append(chat.id, "inbox", {"message_id": second.id})
    await durable.prepare_turn.func(turn)
    assert await events.read(chat.id, "turns") == [(0, {
        "type": "turn.prepared", "turn_id": "turn_1", "message_ids": [first.id],
    })]
    next_turn = turn.model_copy(update={"turn_id": "turn_2"})
    prepared = await durable.prepare_turn.func(next_turn)
    assert prepared.history[-1] == second
    assert (await events.read(chat.id, "turns"))[-1][1]["message_ids"] == [first.id, second.id]


@pytest.mark.parametrize("stored_reply", [False, True])
async def test_cached_worker_reply_keeps_stable_id(stored_reply):
    from store import spaces

    space = await spaces.create("work")
    chat = await chats.create(space.id, "work")
    task = worker.Task(
        id="task_1", chat_id=chat.id, worker_id="wrk_1", title="fix", prompt="fix it",
        model="openai/test", status="complete", event_sequence=2, completion_sequence=2,
        completion_message="done", created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:00:00+00:00",
    )
    await worker.store.save_task(task)
    marker = ai.user_message("worker completed")
    marker.id = "subagent_result_task_1_2"
    reply = ai.assistant_message("done")
    for message in [marker, *([reply] if stored_reply else [])]:
        await events.append(chat.id, "messages", message.model_dump(mode="json"))
    turn = durable.TurnInput(chat_id=chat.id, turn_id="turn_1", origin="worker", task_id=task.id)
    expected = durable.Reply(id=reply.id if stored_reply else "subagent_reply_task_1_2", text="done")
    for _ in range(2):
        assert (await durable.prepare_turn.func(turn)).cached_reply == expected


async def test_no_assistant_text_produces_no_external_placeholder(monkeypatch):
    from app import server

    async def deliver(*args, **kwargs):
        pytest.fail("internal placeholder must not be delivered")

    monkeypatch.setattr(server, "_deliver", deliver)
    message = ai.tool_message(tool_call_id="call_1", result="done", tool_name="work")
    replies = await durable.commit_messages.func("chat_1", [message])
    assert replies == []
    await durable.deliver_replies.func(durable.TurnInput(chat_id="chat_1", origin="ui"), replies)


async def test_completed_turn_drains_after_finish_and_stream_close(monkeypatch):
    from app import server
    from store import turns

    writer = types.SimpleNamespace(closed=False, events=[])
    delivered = []
    drained = []

    async def write(value):
        writer.events.append(value)

    async def close():
        writer.closed = True

    async def prepare(turn):
        return durable.PreparedTurn(history=[], cached_reply=durable.Reply(id="reply_1", text="done"))

    async def deliver(chat_id, text, **kwargs):
        delivered.append((chat_id, text, kwargs))
        return []

    async def drain(chat_id):
        assert await turns.active(chat_id) is None
        assert writer.closed
        assert writer.events[-1]["type"] == "turn.completed"
        drained.append(chat_id)

    async def emit(*args):
        pass

    writer.write = write
    writer.close = close
    monkeypatch.setattr(durable.vercel.workflow, "get_writable", lambda: writer)
    monkeypatch.setattr(durable.vercel.workflow, "get_workflow_metadata", lambda: types.SimpleNamespace(run_id="run_1"))
    monkeypatch.setattr(durable, "prepare_turn", prepare)
    monkeypatch.setattr(durable, "emit_turn_event", emit)
    monkeypatch.setattr(server, "_deliver", deliver)
    monkeypatch.setattr(server, "_run_inbound_turn", drain)
    for name in ("register_turn", "deliver_replies", "finish_turn", "write_lifecycle_event", "close_stream", "drain_inbound"):
        monkeypatch.setattr(durable, name, getattr(durable, name).func)
    await inspect.unwrap(durable.run_turn.func)(
        durable.TurnInput(chat_id="chat_1", turn_id="turn_1", origin="channel"),
    )
    assert delivered == [("chat_1", "done", {"final": True, "message_id": "reply_1"})]
    assert drained == ["chat_1"]


@pytest.mark.parametrize("later_failure", [False, True])
async def test_shared_thread_receipts_keep_live_scope_on_replay(monkeypatch, later_failure):
    from app import server
    from channels import destinations
    from store import spaces

    space = await spaces.create("work")
    space.repos = ["acme/work"]
    await spaces.save(space)
    chat = await chats.create(space.id, "share", user_id="user_test")
    history = [ai.user_message("private request"), ai.assistant_message("private history"),
               ai.user_message("share the summary and continue")]
    before = ai.assistant_message("private planning", ai.messages.ToolCallPart(
        tool_call_id="actual_share_call", tool_name="start_shared_thread",
        tool_args=json.dumps({"provider": "github", "destination": "acme/work#7", "text": "Shared summary"}),
    ))
    after = ai.assistant_message("future public reply")
    for message in history:
        await events.append(chat.id, "messages", message.model_dump(mode="json"))
    requests = []
    mirrors = []

    async def get_user(user_id):
        return {"id": "user_test", "email": "test@vercel.com", "github": {"id": "42", "login": "owner"}}

    def respond(request):
        requests.append((request.method, request.url.path))
        responses = {
            "/repos/acme/work/issues/7": {"number": 7},
            "/repos/acme/work": {"id": 123, "full_name": "acme/work"},
            "/repos/acme/work/issues/7/comments": {"id": 99, "html_url": "https://github.com/acme/work/issues/7#issuecomment-99"},
        }
        return httpx.Response(200, json=responses[request.url.path])

    @contextlib.asynccontextmanager
    async def client(provider):
        async with httpx.AsyncClient(base_url="https://api.github.com/", transport=httpx.MockTransport(respond)) as http:
            yield http

    async def mirror(chat_id, sharing_id):
        record = next(data for _, data in await events.read(chat_id, "sharing") if data["id"] == sharing_id)
        stored = await server._transcript(chat_id)
        assert any(message.id == before.id for message in stored)
        mirrors.append(record)

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(destinations.auth_store, "get_user", get_user)
    monkeypatch.setattr(destinations, "_client", client)
    monkeypatch.setattr(server, "_mirror_sharing", mirror, raising=False)
    async def model_step(context, writer):
        if len(context.messages) == len(history):
            return before
        if later_failure:
            raise RuntimeError("later model step failed")
        return after

    monkeypatch.setattr(durable, "llm_step", model_step)
    commit = durable.commit_messages.func
    share_step = durable.start_shared_thread_step.func
    for name in ("commit_messages", "write_stream_event", "start_shared_thread_step", "mirror_sharing"):
        monkeypatch.setattr(durable, name, getattr(durable, name).func)

    # Replay the same completed model messages. The service's durable receipt
    # prevents another provider post even if a workflow step ran before crashing.
    for _ in range(2):
        agent = durable.DurableDispatcher(chat.id, Writer(), "turn_stable")
        model = ai.get_model("openai/test")
        token = durable.current_agent.set(agent)
        try:
            with pytest.raises(ExceptionGroup) if later_failure else contextlib.nullcontext():
                async with agent.run(model, history) as run:
                    async for _event in run:
                        pass
        finally:
            durable.current_agent.reset(token)

    assert requests.count(("POST", "/repos/acme/work/issues/7/comments")) == 1
    assert len(mirrors) == 2
    assert mirrors[0] == mirrors[1]
    sharing = mirrors[0]
    assert sharing["tool_call_id"] == "actual_share_call"
    assert sharing["state"]["excluded_message_ids"] == [message.id for message in [*history, before]]
    assert after.id not in sharing["state"]["excluded_message_ids"]
    stored = await server._transcript(chat.id)
    assert sum(message.id == before.id for message in stored) == 1
    assert any(message.role == "tool" and message.tool_results[0].tool_call_id == "actual_share_call" for message in stored)
    assert any(message.id == after.id for message in stored) is not later_failure

    # A replayed workflow step may return its cached receipt without re-entering
    # destinations. Mirroring must be a separate step after that cached result.
    receipt = await share_step(
        chat.id, "github", "acme/work#7", "Shared summary", None, delivery_key="turn_stable",
        tool_call_id="actual_share_call", excluded_message_ids=[message.id for message in [*history, before]],
    )

    async def cached_step(*args, **kwargs):
        return receipt

    monkeypatch.setattr(durable, "start_shared_thread_step", cached_step)
    later_failure = False
    agent = durable.DurableDispatcher(chat.id, Writer(), "turn_stable")
    token = durable.current_agent.set(agent)
    try:
        async with agent.run(ai.get_model("openai/test"), history) as run:
            async for _event in run:
                pass
    finally:
        durable.current_agent.reset(token)
    assert len(mirrors) == 3
    assert requests.count(("POST", "/repos/acme/work/issues/7/comments")) == 1
    replies = await commit(chat.id, run.messages[len(history):])
    assert replies == [durable.Reply(id=before.id, text=before.text), durable.Reply(id=after.id, text=after.text)]


async def test_delivery_filters_old_text_and_delivers_every_new_reply_once(monkeypatch):
    from app import server

    chat = await chats.create(None, "share")
    before = ai.assistant_message("private planning before sharing")
    first = ai.assistant_message("first public reply")
    second = ai.assistant_message("second public reply")
    await chats.bind("github:repo:123:issue:7", chat.id, "github", {
        "sharing_id": "sharing_1", "excluded_message_ids": [before.id],
    })
    delivered = []

    class Channel:
        async def on_event(self, event, state):
            delivered.append(event.data["message"])

    monkeypatch.setitem(server.bot.channels, "github", Channel())
    replies = await durable.commit_messages.func(chat.id, [before, first, second])
    turn = durable.TurnInput(chat_id=chat.id, turn_id="turn_1", origin="ui")
    for _ in range(2):
        await durable.deliver_replies.func(turn, replies)
    assert delivered == ["first public reply", "second public reply"]
