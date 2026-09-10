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


async def test_ship_spans_flushes_before_step_exit(monkeypatch):
    calls = []

    async def push_all(spans):
        calls.append(("push", spans))

    monkeypatch.setattr(ai.experimental_telemetry, "push_all", push_all)
    monkeypatch.setattr("agent.telemetry.flush", lambda: calls.append(("flush", None)))

    await durable.ship_spans.func([])

    assert calls == [("push", []), ("flush", None)]


async def test_durable_tools_keep_effects_non_retriable():
    assert durable.llm_step.max_retries > 0
    assert durable.list_sandboxes_step.max_retries > 0
    assert durable.check_subagent_step.max_retries > 0
    assert durable.create_sandbox_step.max_retries == 0
    assert durable.create_subagent_step.max_retries == 0
    assert durable.message_subagent_step.max_retries == 0
    assert durable.require_attention_step.max_retries == 0
    assert durable.start_thread_step.max_retries == 0
    assert durable.read_notes_step.max_retries > 0
    assert durable.create_note_step.max_retries == 0
    assert durable.edit_note_step.max_retries == 0
    assert durable.deliver_replies.max_retries == 0


def test_start_thread_tool_is_removed_for_linked_chats():
    class Writer:
        async def write(self, value):
            pass

    unlinked = durable.DurableDispatcher("chat_1", Writer())
    linked = durable.DurableDispatcher("chat_1", Writer(), linked=True)

    assert "start_thread" in {tool.name for tool in unlinked.tools}
    assert "start_thread" not in {tool.name for tool in linked.tools}


async def test_prepare_turn_detects_linked_chat():
    from store import spaces

    space = await spaces.default()
    chat = await chats.create(space.id, "linked")
    await chats.bind("slack:T1:C1:1.0", chat.id, "slack", {})

    prepared = await durable.prepare_turn.func(
        durable.TurnInput(chat_id=chat.id, origin="ui")
    )

    assert prepared.linked is True
    assert "Reply normally without a notification tool call" in prepared.history[0].text


async def test_prepare_turn_reuses_the_chat_trace():
    from store import spaces

    space = await spaces.default()
    chat = await chats.create(space.id, "traced")
    sink = ai.experimental_telemetry.DictSink()

    async with ai.experimental_telemetry.use_sink(sink):
        first = await durable.prepare_turn.func(
            durable.TurnInput(chat_id=chat.id, turn_id="turn_1", origin="ui")
        )
        second = await durable.prepare_turn.func(
            durable.TurnInput(chat_id=chat.id, turn_id="turn_2", origin="worker")
        )

    assert first.telemetry_span is not None
    assert second.telemetry_span is not None
    assert first.telemetry_span["trace_id"] == second.telemetry_span["trace_id"]
    prepared = [
        span for span in sink.finished_spans if span.name == "hatchery.prepare_turn"
    ]
    assert {span.trace_id for span in prepared} == {first.telemetry_span["trace_id"]}
    assert {span.parent_id for span in prepared} == {first.telemetry_span["id"]}


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


def test_tool_specs_expose_local_operational_guidance_to_the_model():
    tools = {tool.name: tool for tool in [*durable.BASE_TOOLS, durable.start_thread]}
    expected = {
        "create_sandbox": (
            "persistent sandbox owned by this chat",
            "``repos`` selects owner/repo",
            "Use small for research",
            "Files and processes persist",
            "return its metadata",
        ),
        "list_sandboxes": (
            "Return metadata",
            "only reads sandbox state",
            "before creating another sandbox",
        ),
        "create_subagent": (
            "fresh fx subagent chat",
            "``task`` is the context",
            "launch was accepted",
            "not the completed result",
        ),
        "message_subagent": (
            "Queue ``message``",
            "omitting it targets this chat's most recently created subagent",
            "durable queue accepted",
            "older queued work",
            "stale completion does not mean this message failed",
        ),
        "check_subagent": (
            "without changing it",
            "``after`` requests events after a sequence",
            "``limit`` bounds the event count from 1 to 50",
        ),
        "require_attention": (
            "Set and return",
            "``result_available`` exactly when",
            "``blocked`` exactly when",
            "recoverable failures",
        ),
        "find_channels": (
            "without sending a message",
            "``provider`` selects",
            "Return real destination IDs",
        ),
        "find_people": (
            "without sending anything",
            "return person IDs",
            "never invent handles",
        ),
        "start_thread": (
            "Send the first notification",
            "``destination`` must be an exact ID",
            "Returns delivery status",
            "removes this tool from subsequent model steps",
        ),
        "read_notes": (
            "Omit ``filename``",
            "``not_found`` status",
            "does not expose sandbox or repository files",
        ),
        "create_note": (
            "lean durable markdown note",
            "``filename`` is an exact .md name",
            "Returns ``created``, ``exists``, or ``limit_reached``",
        ),
        "edit_note": (
            "Replace one unique snippet",
            "``find`` should use precise complete lines",
            "``missing``, ``ambiguous``, or ``low_confidence``",
            "Read the note and retry",
        ),
    }

    assert set(tools) == set(expected)
    for name, snippets in expected.items():
        description = " ".join(tools[name].tool.spec.description.split())
        for snippet in snippets:
            assert snippet in description

    reason = tools["require_attention"].tool.spec.params["properties"]["reason"]
    assert reason["enum"] == ["result_available", "blocked"]


async def test_note_tools_share_memory_with_the_current_chats_space():
    from store import spaces

    space = await spaces.create("recurring")
    chat = await chats.create(space.id, "scheduled review")

    assert {"read_notes", "create_note", "edit_note"} <= {
        tool.name for tool in durable.BASE_TOOLS
    }
    assert await durable.read_notes_step.func(chat.id, None) == []
    created = await durable.create_note_step.func(
        chat.id, "reviewed_issues.md", "- issue 12 reviewed"
    )
    listed = await durable.read_notes_step.func(chat.id, None)
    read = await durable.read_notes_step.func(chat.id, "reviewed_issues.md")
    updated = await durable.edit_note_step.func(
        chat.id,
        "reviewed_issues.md",
        "- issue 12 reviewed",
        "- issue 12 closed",
    )

    assert created["status"] == "created"
    assert listed == [
        {
            "filename": "reviewed_issues.md",
            "updated_at": created["updated_at"],
        }
    ]
    assert read["content"] == "- issue 12 reviewed"
    assert updated["status"] == "saved"
    assert updated["match"] == "exact"
    assert updated["content"] == "- issue 12 closed"
    properties = durable.edit_note.tool.spec.params["properties"]
    assert set(properties) == {"filename", "find", "replacement"}
    assert "expected_revision" not in properties


async def test_note_tool_rejects_unsafe_find_replace_without_writing():
    from store import spaces

    space = await spaces.create("safe edits")
    chat = await chats.create(space.id, "scheduled review")
    await durable.create_note_step.func(
        chat.id,
        "reviewed_issues.md",
        "duplicate line\nduplicate line\n",
    )

    result = await durable.edit_note_step.func(
        chat.id,
        "reviewed_issues.md",
        "duplicate line",
        "changed",
    )

    assert result["status"] == "ambiguous"
    current = await durable.read_notes_step.func(chat.id, "reviewed_issues.md")
    assert current["content"] == "duplicate line\nduplicate line\n"


async def test_start_thread_uses_trusted_chat_and_turn_ids(monkeypatch):
    calls = []

    async def step(*args):
        calls.append(args)
        return {"status": "sent"}

    class Writer:
        async def write(self, value):
            pass

    monkeypatch.setattr(durable, "start_thread_step", step)
    agent = durable.DurableDispatcher("chat_trusted", Writer(), "turn_1")
    token = durable.current_agent.set(agent)
    try:
        assert await durable.start_thread.fn(
            "slack", "T1/C1", "done", ["user_1"]
        ) == {"status": "sent"}
        assert agent.linked is True
    finally:
        durable.current_agent.reset(token)

    assert calls == [
        ("chat_trusted", "slack", "T1/C1", "done", ["user_1"], "turn_1")
    ]


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
