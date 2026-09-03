import ai

from agent import durable
from store import chats, events
import worker


async def test_durable_tools_keep_effects_non_retriable():
    assert durable.llm_step.max_retries > 0
    assert durable.list_sandboxes_step.max_retries > 0
    assert durable.check_subagent_step.max_retries > 0
    assert durable.create_sandbox_step.max_retries == 0
    assert durable.create_subagent_step.max_retries == 0
    assert durable.message_subagent_step.max_retries == 0
    assert durable.deliver_replies.max_retries == 0


async def test_custom_loop_uses_durable_model_step(monkeypatch):
    calls = []

    async def model_step(model_data, messages_data, tools_data):
        calls.append((model_data, messages_data, tools_data))
        return ai.assistant_message("done").model_dump(mode="json")

    monkeypatch.setattr(durable, "llm_step", model_step)
    agent = durable.DurableDispatcher()
    history = [ai.user_message("help")]
    async with agent.run(ai.get_model("openai/test"), history) as result:
        async for _ in result:
            pass

    assert result.messages[-1].text == "done"
    assert len(calls) == 1
    assert calls[0][1][0]["role"] == "user"
    assert calls[0][2] == []


async def test_commit_messages_is_idempotent():
    message = ai.assistant_message("done")
    payload = [message.model_dump(mode="json")]

    assert await durable.commit_messages.func("chat_1", payload) == ["done"]
    assert await durable.commit_messages.func("chat_1", payload) == ["done"]

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
    turn = durable.TurnInput(
        chat_id=chat.id, origin="worker", task_id=task.id
    ).model_dump(mode="json")
    await durable.deliver_replies.func(turn, ["working", "done"])

    assert delivered == [
        (chat.id, "working", False),
        (chat.id, "done", True),
    ]
    current = await worker.get_task(chat.id, task.id)
    assert current.completion_message == "done"
    assert current.completion_delivered is True
    assert (await chats.get(chat.id)).status == "done"


async def test_start_turn_uses_plain_serializable_input(monkeypatch):
    seen = {}

    class Run:
        run_id = "run_1"

    async def start(workflow, payload):
        seen["workflow"] = workflow
        seen["payload"] = payload
        return Run()

    monkeypatch.setattr(durable.vercel.workflow, "start", start)

    assert await durable.start_turn("chat_1", "worker", "task_1") == "run_1"
    assert seen == {
        "workflow": durable.run_turn,
        "payload": {
            "chat_id": "chat_1",
            "origin": "worker",
            "task_id": "task_1",
        },
    }
