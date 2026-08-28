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

    assert launch == sandbox.Launch(title="docs", repos=["acme/docs"], ports=[3000])
    assert seen["model"] == "openai/gpt-5.6-luna"
    assert seen["output_type"] is sandbox.Launch
    assert seen["params"].output.max_tokens == 4096
    assert "Run the docs site" in seen["messages"][1].parts[0].text


async def test_prepare_creates_default_manual_terminal(monkeypatch):
    created = []

    async def create_terminal(chat_id, devbox_id, title):
        created.append((chat_id, devbox_id, title))

    monkeypatch.setattr(sandbox.terminals, "create", create_terminal)
    record = await sandbox.prepare("chat_1", sandbox.Launch(title="manual"))

    assert created == [("chat_1", record["id"], "bash")]


def test_launch_validates_devbox_api_parameters():
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
