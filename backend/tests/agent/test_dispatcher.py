import models
from agent import dispatcher


def space():
    return models.Space(
        id="spc_docs",
        name="docs",
        about="Keep the SDK documentation accurate.",
        repos=["vercel/vercel-py"],
        resources=[
            models.Resource(
                title="SDK guide",
                kind="documentation",
                url="https://example.com/sdk",
            )
        ],
        color="#38bdf8",
        created_at="2026-08-26T00:00:00+00:00",
    )


def test_system_prompt_describes_trust_and_visibility_boundaries():
    prompt = " ".join(dispatcher.system_prompt(space()).split())

    assert "self-hosted for a known team" in prompt
    assert "access is allowlist-only" in prompt
    for scope in (
        "SPACE",
        "RESOURCE",
        "TRANSCRIPT",
        "NOTES",
        "SANDBOX",
        "REPOSITORY",
        "SUBAGENT CHAT",
        "HUMAN VIEW",
    ):
        assert f"- {scope}" in prompt
    assert "Subagents do not see this transcript" in prompt
    assert "internal <subagent_result> messages are hidden" in prompt
    assert "Notes are not files in a sandbox or repository" in prompt
    assert "vercel/vercel-py" in prompt
    assert "SDK guide (documentation): https://example.com/sdk" in prompt


def test_system_prompt_describes_every_dispatcher_tool():
    prompt = " ".join(dispatcher.system_prompt(space()).split())

    for tool in (
        "list_sandboxes",
        "create_sandbox",
        "create_subagent",
        "message_subagent",
        "check_subagent",
        "require_attention",
        "find_channels",
        "find_people",
        "start_thread",
        "read_notes",
        "create_note",
        "edit_note",
    ):
        assert tool in prompt
    assert "result_available exactly when a result is ready for human review" in prompt
    assert "blocked exactly when progress requires human input" in prompt
    assert "Do not use either for routine progress" in prompt
    assert "one unique existing snippet" in prompt
    assert "low-confidence match" in prompt


def test_system_prompt_presents_coordination_as_tradeoffs():
    prompt = " ".join(dispatcher.system_prompt(space()).split())

    assert "judgment, not a required state machine" in prompt
    assert "roughly over five turns" in prompt
    assert "revisions, backtracking, and rejected directions" in prompt
    assert "Heavy research should usually hand concise findings" in prompt
    assert "Separable work often benefits from independent focused agents" in prompt
    assert "Make handoffs compact" in prompt
    assert "Stopping is flexible" in prompt
    assert "When an agent stalls or fails" in prompt
    assert "Do not turn ordinary recovery into a human blocker" in prompt


def test_unlinked_system_prompt_describes_thread_starting():
    prompt = " ".join(dispatcher.system_prompt(space()).split())

    assert "This chat is not linked to an external thread" in prompt
    assert "start_thread may send the first notification" in prompt
    assert "Use only exact destination and person IDs" in prompt


def test_linked_system_prompt_instructs_inline_reply():
    prompt = dispatcher.system_prompt(space(), linked=True)

    assert "Reply normally without a notification tool call" in prompt
    assert "will be delivered to every linked channel" in prompt
    assert "start_thread may send the first notification" not in prompt
