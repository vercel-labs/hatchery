"""Compaction keeps a long thread inside the model window while the chat stays complete.

Ported from agentmesh `tests/unit/test_agent_context.py` (compaction) and
`tests/integration/test_thread.py` (prospective compaction and hard limits).
"""

import ai
import ai.testing
import pytest

from hatchery import config, model_budget
from hatchery.agent import compaction, supervisor, tools
from hatchery.app import server
from hatchery.store import events

from tests.agent import conftest

call = ai.testing.tool_call


def dump(message: ai.messages.Message) -> dict:
    return message.model_dump(mode="json")


def test_split_never_separates_a_tool_call_from_its_results() -> None:
    asked = call(tools.bash, command="ls")
    history = [
        dump(ai.user_message("start")),
        dump(ai.assistant_message(asked)),
        dump(ai.tool_message(tools.result(asked, {"exit_code": 0}))),
        dump(ai.assistant_message("listed")),
    ]
    head, tail = compaction.split(history, keep=2)
    assert [m["role"] for m in head] == ["user"]
    assert [m["role"] for m in tail] == ["assistant", "tool", "assistant"]
    assert compaction.due(10, 5, above=5, keep=3) is True
    assert compaction.due(10, 4, above=5, keep=3) is False


def test_transcript_labels_sources_and_previous_handoffs() -> None:
    history = [
        dump(ai.user_message(compaction.MARKER + "Old summary.")),
        {**dump(ai.user_message("Check it")), "source": "parent"},
        {**dump(ai.user_message("Signal: later")), "source": "signal"},
        dump(ai.assistant_message("Done.")),
    ]
    assert compaction.transcript(history) == (
        "[previous handoff]\nOld summary.\n[parent] Check it\n[signal] Signal: later\n"
        "[assistant] Done."
    )


async def test_compaction_replaces_the_head_with_a_handoff_and_keeps_the_tail() -> None:
    history = [
        dump(ai.user_message("clone the repo")),
        dump(ai.assistant_message("cloning")),
        dump(ai.user_message("now run tests")),
        dump(ai.assistant_message("running")),
    ]
    model = ai.testing.FakeModel(
        [
            ai.user_message(compaction.transcript(history[:2])),
            ai.assistant_message("Handoff: repo cloned at src/."),
        ]
    )
    compacted, _ = await compaction.compact(
        model, history, keep=2, max_input_tokens=20_000, max_output_tokens=1000
    )
    first = ai.messages.Message.model_validate(compacted[0])
    assert first.role == "user" and first.text.startswith(compaction.MARKER)
    assert "repo cloned at src/" in first.text
    assert compacted[1:] == history[2:]


async def test_compaction_marks_a_loaded_skill_that_left_the_verbatim_tail() -> None:
    viewed = ai.messages.ToolCallPart(
        tool_call_id="call-skill", tool_name="skill_view", tool_args='{"name":"release"}'
    )
    loaded = ai.messages.ToolResultPart(
        tool_call_id="call-skill",
        tool_name="skill_view",
        result={"name": "release", "content": "# Release"},
    )
    history = [
        dump(ai.user_message("prepare a release")),
        dump(ai.assistant_message(viewed)),
        dump(ai.tool_message(loaded)),
        dump(ai.user_message("continue")),
    ]
    model = ai.testing.FakeModel(
        [
            ai.user_message(compaction.transcript(history[:3])),
            ai.assistant_message("Release preparation is active."),
        ]
    )
    compacted, _ = await compaction.compact(
        model, history, keep=1, max_input_tokens=20_000, max_output_tokens=1000
    )
    summary = ai.messages.Message.model_validate(compacted[0]).text
    assert "[SKILL_PRUNED: reload with skill_view(name='release')]" in summary


# Phase 4 gate


async def test_compacted_history_stays_compacted_across_later_turns(
    run: conftest.Run,
) -> None:
    settings = config.Config(
        thread=config.ThreadConfig(max_turns=8, compact_above_tokens=1, keep_recent_messages=1),
        budget=config.BudgetConfig(tokens_per_day=100_000),
    )
    first_summary = compaction.MARKER + "You investigated the job."
    second_summary = compaction.MARKER + "Investigation and follow-up are done."
    scripts = [
        [ai.user_message("Investigate the failing job"), ai.assistant_message("First answer.")],
        # Summaries of the older history.
        [
            ai.user_message("[user] Investigate the failing job\n[assistant] First answer."),
            ai.assistant_message("You investigated the job."),
        ],
        [
            ai.user_message(
                "[previous handoff]\nYou investigated the job.\n[user] second\n"
                "[assistant] Second answer."
            ),
            ai.assistant_message("Investigation and follow-up are done."),
        ],
        # Main calls after each compaction see only the summary and the recent tail.
        [ai.user_message(first_summary), ai.user_message("second"), ai.assistant_message("Second answer.")],
        [ai.user_message(second_summary), ai.user_message("third"), ai.assistant_message("Third answer.")],
    ]
    async with run(*scripts, settings=settings) as app:
        chat_id = await app.chat("Investigate the failing job")
        await app.chat("second", chat_id=chat_id)
        await app.chat("third", chat_id=chat_id)
        assert not app.model.unused

        details = await app.details(chat_id)
        assert details["compactions"] == 2
        model_history = [ai.messages.Message.model_validate(m).text for m in details["messages"]]
        assert model_history == [second_summary, "third", "Third answer."]
        # Later main calls never saw the compacted messages again.
        last_call = [m.text for m in app.model.calls[-1] if m.role != "system"]
        assert last_call == [second_summary, "third"]
        assert all(
            "Investigate the failing job" not in m.text
            for request in app.model.calls[3:]
            for m in request
            if m.role == "user" and not m.text.startswith("[")
        )

        # Operators still see the complete transcript.
        transcript = [m.text for m in await server._transcript(chat_id)]
        assert transcript == [
            "Investigate the failing job",
            "First answer.",
            "second",
            "Second answer.",
            "third",
            "Third answer.",
        ]
        # The ingress cursor sits past everything the thread already has.
        binding = await events.tail(chat_id, "thread")
        assert binding["cursor"] == 4
        assert await supervisor.thread_for_chat(chat_id) == details["thread_id"]


async def test_request_larger_than_the_hard_model_limit_parks_before_inference(
    run: conftest.Run,
) -> None:
    async with run(
        [ai.user_message("x" * 4000), ai.assistant_message("unused")],
        model_limits=model_budget.ModelLimits(6000, 4096),
        strict=False,
    ) as app:
        chat_id = await app.chat("x" * 4000)
        details = await app.details(chat_id)
        assert details["status"] == "parked"
        assert "model context cannot fit" in details["error"]
        assert not app.model.calls


@pytest.mark.parametrize("limit", [1024, 4096])
def test_model_preview_bounds_large_tool_output(limit: int) -> None:
    value = {"exit_code": 0, "stdout": "a" * 50_000, "stderr": "b" * 10_000}
    preview = tools.model_preview(value, limit)
    assert preview is not None and preview["exit_code"] == 0
    assert len(tools._json_bytes(preview)) <= limit
    assert "bytes omitted" in preview["stdout"]
    assert tools.model_preview({"exit_code": 0}, limit) is None
