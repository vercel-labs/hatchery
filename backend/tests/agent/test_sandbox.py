import pytest

import models
from agent import sandbox


def test_launch_has_strict_gateway_schema():
    schema = sandbox.Launch.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


async def test_suggest_uses_luna_and_space_description(monkeypatch):
    seen = {}

    class Run:
        output = sandbox.Launch(title="docs", repos=["acme/docs"], ports=[3000])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Agent:
        def run(self, model, messages, output_type, params):
            seen.update(model=model, messages=messages, output_type=output_type, params=params)
            return Run()

    monkeypatch.setattr(sandbox.ai, "Agent", Agent)
    monkeypatch.setattr(sandbox.ai, "get_model", lambda name: name)
    space = models.Space(
        id="spc_docs",
        name="docs",
        about="Run the docs site on port 3000.",
        repos=["acme/docs"],
        resources=[],
        color="#000000",
        created_at="2026-08-27T00:00:00+00:00",
    )

    launch = await sandbox.suggest(space)

    assert launch == sandbox.Launch(
        title="docs", repos=["acme/docs"], ports=[3000], size="small"
    )
    assert seen["model"] == "openai/gpt-5.6-luna"
    assert seen["output_type"] is sandbox.Launch
    assert seen["params"].output.max_tokens == 4096
    assert "Run the docs site" in seen["messages"][1].parts[0].text


def test_launch_validates_sandbox_parameters():
    launch = sandbox.Launch(
        title="  manual  ",
        repos=["acme/app"],
        setup_script="  pnpm install  ",
        ports=[3000, 8000],
        branch="  main  ",
    )

    assert launch.title == "manual"
    assert launch.setup_script == "pnpm install"
    assert launch.branch == "main"


async def test_sandbox_operations_delegate_with_chat_scope(monkeypatch):
    seen = {}

    async def get(_chat_id):
        return type("Chat", (), {"user_id": "user_1"})()

    async def create(chat_id, spec, *, user_id=None):
        seen.update(chat_id=chat_id, spec=spec, user_id=user_id)
        return "created"

    monkeypatch.setattr(sandbox.chats, "get", get)
    monkeypatch.setattr(sandbox.worker, "create", create)

    result = await sandbox.create("chat_1", sandbox.Launch(repos=["acme/app"]))

    assert result == "created"
    assert seen["chat_id"] == "chat_1"
    assert seen["user_id"] == "user_1"
    assert seen["spec"].repos == ["acme/app"]
    assert seen["spec"].size == "small"
    assert seen["spec"].resolved_resources() == (2, 4096)


def test_launch_accepts_only_supported_sizes():
    assert sandbox.Launch(size="big").size == "big"
    with pytest.raises(ValueError):
        sandbox.Launch(size="medium")


async def test_launch_task_immediately_invalidates_ui(monkeypatch):
    task = sandbox.worker.Task(
        id="task_1",
        chat_id="chat_1",
        worker_id="wrk_1",
        title="fix it",
        prompt="fix it",
        model="openai/test",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )

    async def launch_task(chat_id, sandbox_id, prompt, model):
        assert (chat_id, sandbox_id, prompt, model) == (
            "chat_1", "wrk_1", "fix it", "openai/test"
        )
        return task

    monkeypatch.setattr(sandbox.worker, "launch_task", launch_task)

    created = await sandbox.launch_task("chat_1", "wrk_1", "fix it", "openai/test")

    assert created == task
    assert await sandbox.events.read("chat_1", "ui") == [
        (0, {
            "type": "task.changed",
            "subagent_id": "task_1",
            "sandbox_id": "wrk_1",
            "state": "pending",
        })
    ]
