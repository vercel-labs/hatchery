import json
from unittest import mock

from agent import turn
from channels import protocol
from store import chats, events, projects


async def test_emit_step_appends_and_fans_out(monkeypatch):
    project = await projects.get_default()
    chat, _ = await chats.claim("fake:t1", "fake", project.id, "t", {"k": "v"})
    fake = mock.Mock()
    fake.on_event = mock.AsyncMock()
    monkeypatch.setattr(turn, "registry", lambda: {"fake": fake})

    await turn.emit_step.func(chat.id, "message.completed", "done")

    [(_, data)] = await events.read(chat.id)
    assert data["type"] == "message.completed"
    assert data["data"] == {"message": "done"}
    event, state = fake.on_event.await_args.args
    assert event.type == "message.completed"
    assert state == {"k": "v"}


async def test_context_step_builds_history_and_memory():
    project = await projects.create("sdk")
    await projects.set_memory(project.id, "ship the port")
    chat = await chats.create(project.id, "t")
    await events.append(chat.id, protocol.event(protocol.MESSAGE_RECEIVED, message="hi").model_dump())
    await events.append(chat.id, protocol.event(protocol.MESSAGE_COMPLETED, message="hello").model_dump())

    context = await turn.context_step.func(chat.id)

    assert context["project"] == "sdk"
    assert context["memory"] == "ship the port"
    assert context["history"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    json.dumps(context)  # step i/o must survive the journal


def run_turn_body():
    return turn.run_turn.func.__wrapped__.__wrapped__


async def test_run_turn_routes_parity_to_task(monkeypatch):
    from agent.tasks import parity

    monkeypatch.setattr(turn, "emit_step", mock.AsyncMock())
    monkeypatch.setattr(
        turn,
        "context_step",
        mock.AsyncMock(
            return_value={
                "project": "default",
                "memory": "",
                "repos": [],
                "history": [{"role": "user", "content": "run parity please"}],
            }
        ),
    )
    started = mock.AsyncMock()
    monkeypatch.setattr(turn.vercel.workflow, "start", started)

    assert await run_turn_body()("cht_1") == "parity run started"
    started.assert_awaited_once_with(parity.parity_workflow, "cht_1")


async def test_run_turn_replies_via_llm(monkeypatch):
    emit = mock.AsyncMock()
    monkeypatch.setattr(turn, "emit_step", emit)
    monkeypatch.setattr(
        turn,
        "context_step",
        mock.AsyncMock(
            return_value={
                "project": "sdk",
                "memory": "current direction",
                "repos": ["vercel/workflow"],
                "history": [{"role": "user", "content": "what are we doing?"}],
            }
        ),
    )
    llm = mock.AsyncMock(return_value="porting tests")
    monkeypatch.setattr(turn, "llm_step", llm)

    assert await run_turn_body()("cht_1") == "porting tests"

    messages_data = llm.await_args.args[0]
    assert len(messages_data) == 2  # system + the user message
    assert "current direction" in json.dumps(messages_data)
    assert emit.await_args_list[0].args == ("cht_1", protocol.TURN_STARTED, "")
    assert ("cht_1", protocol.MESSAGE_COMPLETED, "porting tests") in [c.args for c in emit.await_args_list]
    assert ("cht_1", protocol.TURN_COMPLETED, "") in [c.args for c in emit.await_args_list]


async def test_run_turn_failure_emits_turn_failed(monkeypatch):
    emit = mock.AsyncMock()
    monkeypatch.setattr(turn, "emit_step", emit)
    monkeypatch.setattr(turn, "context_step", mock.AsyncMock(side_effect=RuntimeError("db down")))

    try:
        await run_turn_body()("cht_1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the failure to propagate")
    assert emit.await_args_list[-1].args == ("cht_1", protocol.TURN_FAILED, "RuntimeError: db down")
