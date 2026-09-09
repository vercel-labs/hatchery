import models
from agent import dispatcher


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


def test_system_prompt_describes_worker_and_thread_flow():
    prompt = dispatcher.system_prompt(space())
    assert "Sandboxes are durable" in prompt
    assert "create_subagent" in prompt
    assert "Choose small" in prompt
    assert "Choose big" in prompt
    assert "require_attention with result_available" in prompt
    assert "blocked when work cannot continue" in prompt
    assert "call start_thread" in prompt
    assert "vercel/vercel-py" in prompt


def test_linked_system_prompt_instructs_inline_reply():
    prompt = dispatcher.system_prompt(space(), linked=True)
    assert "Reply normally without a notification tool call" in prompt
    assert "call start_thread" not in prompt
