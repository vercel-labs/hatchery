"""The agent's daily budget: pre-call admission, holds, grants, and the UTC reset.

Ported from agentmesh `tests/integration/test_thread.py` (budget scenarios).
"""

import ai
import ai.testing

from hatchery import messages
from hatchery.agent import budget, supervisor, thread, tools
from hatchery.app import server
from hatchery.store import events, turns

from tests.agent import conftest

call = ai.testing.tool_call


def test_grants_apply_once_and_a_new_utc_day_resets_the_counters() -> None:
    day = budget.Budget(ceiling=1000)
    assert day.roll(10 * budget.DAY + 5) is True
    day.spend(1000)
    assert day.exhausted
    assert day.grant("g1", 500) is True
    assert day.grant("g1", 500) is False  # a retried grant is ignored
    assert (day.limit, day.remaining, day.exhausted) == (1500, 500, False)
    assert day.roll(10 * budget.DAY + 100) is False
    assert day.roll(11 * budget.DAY) is True
    assert (day.spent, day.granted, day.grants) == (0, 0, [])
    assert day.next_day_at() == 12 * budget.DAY


# Phase 4 gate


async def test_exhausted_budget_holds_the_thread_until_a_grant_resumes_it(
    run: conftest.Run,
) -> None:
    script = [
        ai.user_message("first"),
        ai.assistant_message("First answer."),
        ai.user_message("second"),
        ai.assistant_message("Second answer."),
    ]
    async with run(script) as app:
        chat_id = await app.chat("first")
        agent = supervisor.process_id(conftest.AGENT)
        await app.rt.client.send(agent, messages.Spent(1000))
        await app.rt.drain()
        assert (await app.budget())["exhausted"] is True

        # The next input is accepted but no model call runs without admission.
        await app.chat("second", chat_id=chat_id)
        details = await app.details(chat_id)
        assert details["status"] == "active" and details["error"] == ""
        assert details["activity"]["budget_held"] is True
        assert details["activity"]["sandbox_active"] is False, "held threads release the sandbox"
        assert len(app.model.calls) == 1
        roster = await app.roster()
        assert roster["waiting"] == [details["thread_id"]]
        assert roster["threads"][0]["status"] == "waiting"
        # The held turn ends with an explanation instead of hanging the chat.
        assert await turns.active(chat_id) is None
        ended = [data for _, data in await events.read(chat_id, "turns")][-1]
        assert (ended["type"], ended["error"]) == ("turn.failed", thread.BUDGET_HELD)

        await app.rt.client.send(agent, messages.Grant("grant-1", 500))
        await app.rt.drain()
        details = await app.details(chat_id)
        assert details["status"] == "idle"
        assert details["activity"]["budget_held"] is False
        assert (await app.roster())["waiting"] == []
        assert ((await app.budget())["limit"], (await app.budget())["exhausted"]) == (1500, False)
        assert not app.model.unused
        # The resumed work answered in the chat under the thread's own turn.
        assert (await server._transcript(chat_id))[-1].text == "Second answer."
        assert await turns.active(chat_id) is None

        await app.rt.advance("1d")
        assert (await app.budget())["spent"] == 0


async def test_compaction_calls_are_admitted_and_counted(run: conftest.Run, monkeypatch) -> None:
    """A summary call spends budget, so it can hold the main call that follows it."""
    from hatchery import config
    from hatchery.agent import compaction

    summary = ai.user_message(compaction.MARKER + "Earlier work is done.")

    async def compact(model, history, **kwargs):
        return [summary.model_dump(mode="json"), history[-1]], ai.types.usage.Usage(
            input_tokens=1
        )

    monkeypatch.setattr(compaction, "compact", compact)
    settings = config.Config(
        thread=config.ThreadConfig(compact_above_tokens=1, keep_recent_messages=1),
        budget=config.BudgetConfig(tokens_per_day=1000),
    )
    scripts = [
        [ai.user_message("first"), ai.assistant_message(call(tools.idle, note="first done"))],
        [summary, ai.user_message("next"), ai.assistant_message("Next answer.")],
    ]
    async with run(*scripts, settings=settings) as app:
        chat_id = await app.chat("first")
        await app.rt.client.send(supervisor.process_id(conftest.AGENT), messages.Spent(999))
        await app.rt.drain()
        await app.chat("next", chat_id=chat_id)
        details = await app.details(chat_id)
        assert details["compactions"] == 1
        assert (await app.budget())["spent"] == 1000
        assert (await app.roster())["waiting"] == [details["thread_id"]]
        assert len(app.model.calls) == 1, "the main call needs its own admission"

        await app.rt.client.send(
            supervisor.process_id(conftest.AGENT), messages.Grant("after-compaction", 100)
        )
        await app.rt.drain()
        assert (await app.details(chat_id))["status"] == "idle"
        assert not app.model.unused
