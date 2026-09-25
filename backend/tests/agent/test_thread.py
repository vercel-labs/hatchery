"""The durable thread under Rotor's LocalRuntime, with real local Git, a scripted model,
and the scripted sandbox provider. Ported from agentmesh `tests/integration/test_thread.py`
plus Hatchery's chat projection (turns, transcript, channel delivery, fx tasks).
"""

import asyncio
import typing

import ai
import ai.testing
import httpx
import pytest
import rotor.testing

from hatchery import messages, model_budget
from hatchery.agent import supervisor, thread, tools
from hatchery.app import server
from hatchery.store import chats, events, turns
from hatchery.worker import provider, scripted
from hatchery import worker
from hatchery.workspace import files as workspace_files
from hatchery.workspace import review as workspace_review

from tests.agent import conftest

call = ai.testing.tool_call
ASK = "Investigate the failing job"


# Phase 3 gate


async def test_chat_edits_publishes_sleeps_resumes_and_sees_another_chats_change(
    run: conftest.Run, repo
) -> None:
    first = [
        ai.user_message("Refine your persona"),
        ai.assistant_message(
            "Updating AGENTS.md.",
            call(tools.bash, command="edit-agents", description="edit persona"),
            call(tools.idle, note="persona refined"),
        ),
        ai.user_message("What does core memory say now?"),
        ai.assistant_message("It records the release cadence."),
    ]
    second = [
        ai.user_message("Remember the release cadence"),
        ai.assistant_message(
            call(tools.bash, command="edit-memory", description="remember"),
            call(tools.idle, note="remembered"),
        ),
    ]
    commands = {
        "edit-agents": conftest.edit("self/AGENTS.md", "Be precise, brief, and preserve memory.\n"),
        "edit-memory": conftest.edit(
            "self/MEMORY.md", "# Core memory\nReleases ship on Tuesdays.\n"
        ),
    }
    async with run(first, second, commands=commands) as app:
        chat_a = await app.chat("Refine your persona")
        details = await app.details(chat_a)
        assert details["status"] == "idle", details
        # Workspace changes auto-merge; the wiki had nothing to propose.
        assert [(p["section"], p["merged"]) for p in details["proposals"]] == [
            ("workspace", True)
        ]

        # The edit is checkpointed on the thread branch and published to main.
        _, main = await repo.read_main(conftest.AGENT)
        assert main["self/AGENTS.md"].content == b"Be precise, brief, and preserve memory.\n"

        # The turn ended and the chat transcript has everything, in order.
        assert await turns.active(chat_a) is None
        transcript = await server._transcript(chat_a)
        assert [message.role for message in transcript] == ["user", "assistant", "tool"]
        assert transcript[1].text == "Updating AGENTS.md."

        # Warm for the idle grace period, then stopped (not destroyed).
        sandbox = app.sandbox(details)
        assert sandbox.active
        await app.rt.advance(f"{conftest.CONFIG.thread.sandbox_idle_seconds}s")
        assert not sandbox.active
        assert details["sandbox"] in app.sandboxes.sandboxes
        assert (await app.details(chat_a))["activity"]["sandbox_active"] is False
        assert {"type": "sandbox.changed"} in [data for _, data in await events.read(chat_a, "ui")]

        # Another chat of the same agent publishes a memory change.
        chat_b = await app.chat("Remember the release cadence")
        assert (await app.details(chat_b))["status"] == "idle"
        _, main = await repo.read_main(conftest.AGENT)
        assert b"Tuesdays" in main["self/MEMORY.md"].content

        # The first chat resumes its stopped sandbox and sees the merged change.
        await app.chat("What does core memory say now?", chat_id=chat_a)
        details = await app.details(chat_a)
        assert details["status"] == "idle"
        assert app.sandbox(details).active
        assert app.sandbox(details).files["self/MEMORY.md"].content.endswith(
            b"Releases ship on Tuesdays.\n"
        )
        system = app.model.calls[-1][0].text
        assert "Releases ship on Tuesdays." in system
        assert "Be precise, brief, and preserve memory." in system
        assert app.sandboxes.seeds == 2  # one per chat; the resume reused the stopped sandbox
        assert not app.model.unused
        transcript = await server._transcript(chat_a)
        assert [message.text for message in transcript][-2:] == [
            "What does core memory say now?",
            "It records the release cadence.",
        ]


# Sandbox and workspace


async def test_empty_input_allocates_no_sandbox_and_runs_no_model(run: conftest.Run) -> None:
    async with run([ai.user_message(ASK), ai.assistant_message("unused")]) as app:
        chat_id = (await chats.create(conftest.AGENT, "empty")).id
        await events.append(chat_id, "messages", ai.user_message("   ").model_dump(mode="json"))
        turn = await supervisor.start_turn(chat_id, "ui")
        await app.rt.drain()
        assert not app.sandboxes.sandboxes
        assert not app.model.calls
        assert await turns.active(chat_id) is None
        assert (await app.details(chat_id))["status"] == "idle"
        assert turn.run_id == await app.thread_id(chat_id)


@pytest.mark.parametrize("repo", [False], indirect=True)
async def test_fresh_storage_without_wiki_gets_it_from_the_first_reviewed_proposal(
    run: conftest.Run, repo
) -> None:
    """As in agentmesh, `wiki/PROMPT.md` and `wiki/skills/` are optional: a thread on a
    main without `wiki/` still loads, and the first reviewed wiki proposal creates it."""
    script = [
        ai.user_message("Write the team prompt"),
        ai.assistant_message(
            call(tools.bash, command="prompt", description="team prompt"),
            call(tools.idle, note="proposed the team prompt"),
        ),
    ]
    commands = {"prompt": conftest.edit("wiki/PROMPT.md", "Prefer small changes.\n")}
    async with run(script, commands=commands) as app:
        chat_id = await app.chat("Write the team prompt")
        details = await app.details(chat_id)
        assert details["status"] == "idle"
        assert "wiki/PROMPT.md" in app.model.calls[0][0].text, "empty team prompt noted"
        [proposal] = [p for p in details["proposals"] if p["section"] == "wiki"]
        assert proposal["merged"] is False, "wiki changes need review"
        _, main = await repo.read_main(conftest.AGENT)
        assert "wiki/PROMPT.md" not in main

        await workspace_review.LocalReview(repo).merge(proposal["branch"])
        _, main = await repo.read_main(conftest.AGENT)
        assert main["wiki/PROMPT.md"].content == b"Prefer small changes.\n"


async def test_bash_then_signal_pushes_the_tree_and_a_lost_sandbox_is_reseeded_from_it(
    run: conftest.Run, repo
) -> None:
    script = [
        ai.user_message(ASK),
        ai.assistant_message(
            "Diagnosing.",
            call(tools.bash, command="diagnose"),
            call(tools.bash, command="repair"),
            call(tools.signal, note="check the repair", delay="13s"),
            call(tools.idle, note="waiting to check the repair"),
        ),
        ai.user_message("Signal: check the repair"),
        ai.assistant_message(call(tools.bash, command="verify"), call(tools.idle, note="ok")),
        ai.user_message("check again"),
        ai.assistant_message("Still fixed."),
    ]
    commands = {
        "diagnose": provider.ExecResult(9, "partial", "distinct failure"),
        "repair": conftest.edit("self/notes.md", "Remember the repair\n", "fixed"),
        "verify": provider.ExecResult(0, "verified"),
    }
    sandboxes = scripted.ScriptedSandboxProvider(commands, persist=False)
    async with run(script, sandboxes=sandboxes) as app:
        chat_id = await app.chat(ASK)
        details = await app.details(chat_id)
        assert details["status"] == "idle", details
        assert details["signals"][0]["at"] == app.rt.clock.now() + 13
        assert {"exit_code": 9, "stdout": "partial", "stderr": "distinct failure"} in (
            app.tool_results(details)
        )
        pushed = await repo.materialize(conftest.AGENT, details["branch"])
        assert pushed["self/notes.md"].content == b"Remember the repair\n"

        # Activity within the idle grace reuses the sandbox.
        await app.rt.advance("13s")
        details = await app.details(chat_id)
        assert details["signals"] == [] and details["status"] == "idle"
        assert app.sandboxes.seeds == 1

        # After the grace the sandbox is lost; the next input reseeds it from Git.
        await app.rt.advance(f"{conftest.CONFIG.thread.sandbox_idle_seconds}s")
        assert not app.sandboxes.sandboxes
        await app.chat("check again", chat_id=chat_id)
        assert app.sandboxes.seeds == 2
        details = await app.details(chat_id)
        assert app.sandbox(details).files["self/notes.md"].content == b"Remember the repair\n"
        assert not app.model.unused

        # The signal ran as the thread's own turn and its input is in the transcript.
        transcript = await server._transcript(chat_id)
        signal = next(m for m in transcript if m.text == "Signal: check the repair")
        assert signal.provider_metadata["hatchery"] == {"origin": "thread", "source": "signal"}
        started = [data for _, data in await events.read(chat_id, "turns")]
        assert [data["origin"] for data in started if data["type"] == "turn.started"] == [
            "ui",
            "thread",
            "ui",
        ]
        assert await turns.active(chat_id) is None


async def test_large_bash_output_is_full_in_history_and_bounded_for_the_model(
    run: conftest.Run,
) -> None:
    full = "begin-" + "x" * 80_000 + "-end"
    script = [
        ai.user_message(ASK),
        ai.assistant_message(call(tools.bash, command="verbose-diagnostic")),
        ai.assistant_message(call(tools.idle, note="diagnostic inspected")),
    ]
    async with run(
        script,
        commands={"verbose-diagnostic": provider.ExecResult(0, full, "")},
        model_limits=model_budget.ModelLimits(200_000, 10_000),
    ) as app:
        chat_id = await app.chat(ASK)
        details = await app.details(chat_id)
        result = next(
            part
            for message in map(ai.messages.Message.model_validate, details["messages"])
            for part in message.tool_results
        )
        sent = next(part for part in app.model.calls[1][-1].tool_results)
        assert result.result["stdout"] == full
        assert len(sent.get_model_input()["stdout"]) < len(full)
        assert "bytes omitted" in sent.get_model_input()["stdout"]


async def test_skill_view_loads_the_indexed_guide_without_running_a_shell(
    run: conftest.Run,
) -> None:
    script = [
        ai.user_message(ASK),
        ai.assistant_message(call(tools.skill_view, name="release")),
        ai.assistant_message(call(tools.idle, note="release guide loaded")),
    ]
    async with run(script) as app:
        details = await app.details(await app.chat(ASK))
        viewed = next(
            result
            for result in app.tool_results(details)
            if isinstance(result, dict) and result.get("name") == "release"
        )
        assert viewed["path"] == "skills/release/SKILL.md"
        assert viewed["content"].endswith("# Release\n")
        assert viewed["files"] == ["references/checks.md"]
        assert app.sandbox(details).commands == []


async def test_idle_handoff_merges_workspace_and_proposes_wiki(run: conftest.Run, repo) -> None:
    main_sha, _ = await repo.read_main(conftest.AGENT)
    edits = {
        "self/memory.md": workspace_files.File(b"raw thread notes\n"),
        "wiki/guide.md": workspace_files.File(b"raw wiki contribution\n"),
    }
    script = [
        ai.user_message(ASK),
        ai.assistant_message(
            call(tools.bash, command="remember"), call(tools.idle, note="Curate the discovery")
        ),
    ]
    commands = {"remember": scripted.ScriptedCommand(provider.ExecResult(0), writes=edits)}
    async with run(script, commands=commands) as app:
        details = await app.details(await app.chat(ASK))
        assert details["status"] == "idle"
        assert details["base_sha"] == main_sha
        assert details["summary"] == "Curate the discovery"
        proposals = details["proposals"]
        assert [(p["section"], p["merged"]) for p in proposals] == [
            ("workspace", True),
            ("wiki", False),
        ]
        _, merged = await repo.read_main(conftest.AGENT)
        assert merged["self/memory.md"].content == b"raw thread notes\n"
        assert merged["wiki/guide.md"].content == b"Original shared guide.\n"
        wiki = await repo.inspect_proposal(proposals[1]["branch"])
        assert wiki.changes["wiki/guide.md"].content == b"raw wiki contribution\n"


async def test_api_change_uses_serve_review_policy(run: conftest.Run, repo) -> None:
    script = [
        ai.user_message(ASK),
        ai.assistant_message(
            call(tools.bash, command="publish-route"), call(tools.idle, note="route ready")
        ),
    ]
    commands = {
        "publish-route": conftest.edit("self/api/webhook/route.py", "def POST(request): pass\n")
    }
    async with run(script, commands=commands) as app:
        details = await app.details(await app.chat(ASK))
        assert [(p["section"], p["merged"]) for p in details["proposals"]] == [
            ("workspace", False)
        ]
        _, main = await repo.read_main(conftest.AGENT)
        assert "self/api/webhook/route.py" not in main


async def test_a_thread_can_request_but_not_read_an_agent_secret(run: conftest.Run) -> None:
    script = [
        ai.user_message(ASK),
        ai.assistant_message(
            call(
                tools.secret_request,
                name="STRIPE_WEBHOOK_SECRET",
                note="Paste the endpoint signing secret from Stripe.",
            ),
            call(tools.secret_request, name="lowercase", note="bad name"),
            call(tools.idle, note="waiting for configuration"),
        ),
    ]
    async with run(script) as app:
        details = await app.details(await app.chat(ASK))
        assert (await app.roster())["secret_requests"] == {
            "STRIPE_WEBHOOK_SECRET": "Paste the endpoint signing secret from Stripe."
        }
        requested, rejected = app.tool_results(details)[:2]
        assert requested == {"name": "STRIPE_WEBHOOK_SECRET", "status": "requested"}
        assert "uppercase" in str(rejected)
        await supervisor.send(conftest.AGENT, messages.SecretProvided("STRIPE_WEBHOOK_SECRET"))
        await app.rt.drain()
        assert (await app.roster())["secret_requests"] == {}


async def test_running_fx_subagent_defers_the_idle_sandbox_stop(run: conftest.Run) -> None:
    script = [ai.user_message(ASK), ai.assistant_message("Started the work.")]
    async with run(script) as app:
        chat_id = await app.chat(ASK)
        details = await app.details(chat_id)
        task = worker.Task(
            id="task_1",
            chat_id=chat_id,
            worker_id=details["sandbox"].removeprefix("hatchery-"),
            title="fix",
            prompt="fix it",
            model="openai/test",
            status="running",
            created_at="2026-09-03T00:00:00+00:00",
            updated_at="2026-09-03T00:00:00+00:00",
        )
        await worker.store.save_task(task)
        idle = conftest.CONFIG.thread.sandbox_idle_seconds
        await app.rt.advance(f"{idle}s")
        assert app.sandbox(details).active, "a running fx task keeps the sandbox up"
        task.status = "complete"
        await worker.store.save_task(task)
        await app.rt.advance(f"{idle}s")
        assert not app.sandbox(details).active


# Turns, steering, and failure


async def test_a_second_turn_folds_into_the_running_activation(run: conftest.Run) -> None:
    script = [
        ai.user_message("first"),
        ai.user_message("second"),
        ai.assistant_message("Both handled."),
    ]
    async with run(script) as app:
        chat_id = await app.chat("first", drain=False)
        await app.chat("second", chat_id=chat_id, drain=False)
        await app.rt.drain()
        assert len(app.model.calls) == 1
        assert [m.text for m in app.model.calls[0][1:]] == ["first", "second"]
        assert await turns.active(chat_id) is None
        ended = [data for _, data in await events.read(chat_id, "turns")]
        assert [data["type"] for data in ended].count("turn.completed") == 2
        transcript = await server._transcript(chat_id)
        assert [m.text for m in transcript] == ["first", "second", "Both handled."]


class SteeringProvider(scripted.ScriptedSandboxProvider):
    """Posts a second chat message while the `slow` command runs."""

    chat_id = ""

    async def acquire(self, name: str, **kwargs: typing.Any) -> provider.Acquired:
        acquired = await super().acquire(name, **kwargs)
        box, original = acquired.sandbox, acquired.sandbox.exec

        async def exec(command: str, **options: typing.Any) -> provider.ExecResult:
            if command == "slow" and self.chat_id:
                chat_id, self.chat_id = self.chat_id, ""
                message = ai.user_message("also check the logs")
                await events.append(chat_id, "messages", message.model_dump(mode="json"))
                await supervisor.start_turn(chat_id, "ui")
            return await original(command, **options)

        box.exec = exec  # type: ignore[method-assign]
        return acquired


async def test_a_prompt_during_the_turn_is_answered_right_after_it(run: conftest.Run) -> None:
    """Input that lands while tools run or the handoff publishes gets the next turn."""
    script = [
        ai.user_message(ASK),
        ai.assistant_message(call(tools.bash, command="slow"), call(tools.idle, note="done")),
        ai.user_message("also check the logs"),
        ai.assistant_message("Logs are clean."),
    ]
    sandboxes = SteeringProvider({"slow": provider.ExecResult(1, "", "boom")})
    async with run(script, sandboxes=sandboxes) as app:
        sandboxes.chat_id = (await chats.create(conftest.AGENT, "steer")).id
        chat_id = await app.chat(ASK, chat_id=sandboxes.chat_id)
        details = await app.details(chat_id)
        assert details["status"] == "idle"
        assert {"exit_code": 1, "stdout": "", "stderr": "boom"} in app.tool_results(details)
        assert not app.model.unused
        assert await turns.active(chat_id) is None
        transcript = await server._transcript(chat_id)
        assert [m.text for m in transcript if m.role == "user"] == [ASK, "also check the logs"]
        assert transcript[-1].text == "Logs are clean."


async def test_plain_reply_is_delivered_to_linked_channels_once(
    run: conftest.Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered = []

    async def deliver(chat_id, text, *, final=True, delivery_key=None):
        delivered.append((chat_id, text, final, delivery_key))
        return []

    monkeypatch.setattr(server, "_deliver", deliver)
    async with run([ai.user_message("help"), ai.assistant_message("done")]) as app:
        chat_id = await app.chat("help", turn_id="turn_1", drain=False)
        await supervisor.start_turn(chat_id, "ui", turn_id="turn_1")  # a replayed ingress
        await app.rt.drain()
        assert delivered == [(chat_id, "done", True, "turn_1:0")]
        assert len(app.model.calls) == 1


async def test_linked_chat_hides_start_thread_and_tools_run_with_the_chat_context(
    run: conftest.Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    async def list_all(chat_id):
        seen.append(chat_id)
        return []

    monkeypatch.setattr("hatchery.agent.sandbox.list_all", list_all)
    offered: list[set[str]] = []
    stream = ai.stream

    def capture(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        offered.append({tool.name for tool in kwargs["tools"]})
        return stream(*args, **kwargs)

    monkeypatch.setattr(ai, "stream", capture)
    script = [
        ai.user_message("inspect"),
        ai.assistant_message(call(tools.list_sandboxes)),
        ai.assistant_message("inspected"),
    ]
    async with run(script) as app:
        chat_id = (await chats.create(conftest.AGENT, "linked")).id
        await chats.bind("slack:T1:C1:1", chat_id, "slack", {})
        await app.chat("inspect", chat_id=chat_id)
        assert seen == [chat_id]
        assert all("start_thread" not in names for names in offered)
        assert {"bash", "create_subagent", "delegate", "idle"} <= offered[0]
        assert "complete" not in offered[0]
        transcript = await server._transcript(chat_id)
        assert [m.role for m in transcript] == ["user", "assistant", "tool", "assistant"]
        assert transcript[2].tool_results[0].result == []


class FlakyStream:
    """`ai.stream` that fails with a transport error the first `failures` times."""

    def __init__(self, failures: int) -> None:
        self.failures, self.attempts, self.original = failures, 0, ai.stream

    def __call__(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise httpx.ConnectError("gateway reset the connection")
        return self.original(*args, **kwargs)


async def test_a_transient_model_failure_retries_the_turn_after_a_delay(
    run: conftest.Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = FlakyStream(failures=1)
    monkeypatch.setattr(ai, "stream", flaky)
    script = [ai.user_message(ASK), ai.assistant_message("recovered")]
    async with run(script) as app:
        chat_id = await app.chat(ASK)
        assert (await app.details(chat_id))["status"] == "active"
        assert await turns.active(chat_id) is not None
        await app.rt.advance("5s")
        details = await app.details(chat_id)
        assert details["status"] == "idle" and details["turns"] == 1
        assert flaky.attempts == 2
        assert (await server._transcript(chat_id))[-1].text == "recovered"


async def test_repeated_transient_failures_park_and_end_the_turn(
    run: conftest.Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai, "stream", FlakyStream(failures=10))
    async with run([ai.user_message(ASK), ai.assistant_message("never")], strict=False) as app:
        chat_id = await app.chat(ASK)
        await app.rt.advance("5s")
        await app.rt.advance("5s")
        details = await app.details(chat_id)
        assert details["status"] == "parked"
        assert "gateway reset the connection" in details["error"]
        assert await turns.active(chat_id) is None
        failed = [data for _, data in await events.read(chat_id, "turns")][-1]
        assert failed["type"] == "turn.failed"


class OutageProvider(scripted.ScriptedSandboxProvider):
    """The first acquire fails like a provider outage would."""

    outages = 1

    async def acquire(self, name: str, **kwargs: typing.Any) -> provider.Acquired:
        if self.outages:
            self.outages -= 1
            raise RuntimeError("sandbox service unavailable")
        return await super().acquire(name, **kwargs)


async def test_a_failed_activation_parks_the_thread_and_resume_retries_it(
    run: conftest.Run,
) -> None:
    script = [ai.user_message(ASK), ai.assistant_message(call(tools.idle, note="done"))]
    async with run(script, sandboxes=OutageProvider(), strict=False) as app:
        chat_id = await app.chat(ASK)
        details = await app.details(chat_id)
        assert details["status"] == "parked"
        assert "sandbox service unavailable" in details["error"]
        assert not app.model.calls
        assert await turns.active(chat_id) is None
        await app.send(chat_id, messages.Resume())
        details = await app.details(chat_id)
        assert details["status"] == "idle", details
        assert not app.model.unused


async def test_resume_extends_the_turn_ceiling_of_a_parked_thread(run: conftest.Run) -> None:
    script = [
        ai.user_message(ASK),
        *(ai.assistant_message(call(tools.bash, command=f"step-{index}")) for index in range(4)),
        ai.assistant_message(call(tools.idle, note="continued after review")),
    ]
    commands = {f"step-{index}": provider.ExecResult(0, f"result-{index}") for index in range(4)}
    async with run(script, commands=commands) as app:
        chat_id = await app.chat(ASK)
        details = await app.details(chat_id)
        assert details["status"] == "parked"
        assert details["error"] == thread.TURN_CEILING
        assert details["turns"] == details["ceiling"] == 4
        await app.send(chat_id, messages.Resume())
        details = await app.details(chat_id)
        assert details["status"] == "idle", details
        assert details["turns"] == 5 and details["ceiling"] == 8
        assert not app.model.unused


# Turn projection tasks


async def _worker_task(chat_id: str, **fields: typing.Any) -> worker.Task:
    task = worker.Task(
        chat_id=chat_id,
        worker_id="wrk_1",
        title="fix",
        prompt="fix it",
        model="openai/test",
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:00:00+00:00",
        **fields,
    )
    await worker.store.save_task(task)
    return task


async def test_finish_turn_completes_a_worker_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = await chats.create(conftest.AGENT, "worker")
    task = await _worker_task(
        chat.id, id="task_1", status="complete", event_sequence=2, completion_sequence=2
    )
    monkeypatch.setattr(server, "_deliver", lambda *_a, **_k: asyncio.sleep(0, result=[]))
    final = ai.assistant_message("done")
    async with rotor.testing.LocalRuntime(thread.finish_turn) as rt:
        handle = await rt.client.start(
            thread.finish_turn,
            input={
                "chat_id": chat.id,
                "turn_id": "turn_1",
                "process_id": "thread_1",
                "state": "completed",
                "task_id": task.id,
                "replies": ["done"],
                "messages": [final.model_dump(mode="json")],
            },
        )
        await rt.drain()
        assert (await handle.snapshot()).terminal_status == "completed"
    current = await worker.get_task(chat.id, task.id)
    assert current.completion_message == "done" and current.completion_delivered is True
    assert (await chats.get(chat.id)).status == "done"
    assert (await server._transcript(chat.id))[-1].text == "done"
    assert [data["type"] for _, data in await events.read(chat.id, "turns")] == [
        "turn.completed"
    ]


async def test_finish_turn_marks_the_latest_worker_record_without_overwriting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await chats.create(conftest.AGENT, "worker")
    task = await _worker_task(
        chat.id, id="task_race", status="complete", event_sequence=2, result={"summary": "old"}
    )
    original = worker.store.mutate_task

    async def mutate_task(task_id, mutate):
        def receive_newer_event(current):
            current.status = "attention"
            current.event_sequence = 3
            current.result = {"question": "newer"}
            return current

        await original(task_id, receive_newer_event)
        return await original(task_id, mutate)

    monkeypatch.setattr(worker.store, "mutate_task", mutate_task)
    monkeypatch.setattr(server, "_deliver", lambda *_a, **_k: asyncio.sleep(0, result=[]))
    await thread.finish_turn.fn(chat.id, "turn_1", "thread_1", "completed", task.id, None, ["done"])
    current = await worker.get_task(chat.id, task.id)
    assert current.status == "attention" and current.event_sequence == 3
    assert current.result == {"question": "newer"} and current.completion_delivered is True


async def test_finish_turn_retries_delivery_with_the_same_receipt_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await chats.create(conftest.AGENT, "retry")
    attempts = []

    async def deliver(_chat_id, _text, *, final=True, delivery_key=None):
        attempts.append((final, delivery_key))
        return ["slack: unavailable"] if len(attempts) == 1 else []

    monkeypatch.setattr(server, "_deliver", deliver)
    final = ai.assistant_message("done")
    async with rotor.testing.LocalRuntime(thread.finish_turn) as rt:
        handle = await rt.client.start(
            thread.finish_turn,
            input={
                "chat_id": chat.id,
                "turn_id": "turn_1",
                "process_id": "thread_1",
                "state": "completed",
                "replies": ["done"],
                "messages": [final.model_dump(mode="json")],
            },
        )
        await rt.drain()
        await rt.advance("3s")
        assert (await handle.snapshot()).terminal_status == "completed"
    assert attempts == [(True, "turn_1:0"), (True, "turn_1:0")]
    assert len(await events.read(chat.id, "messages")) == 1


async def test_register_turn_is_idempotent_and_owned_by_one_thread() -> None:
    turn = messages.TurnInput("chat_1", "turn_1", "ui")
    assert await thread.register_turn(turn, "thread_1") == 0
    assert await thread.register_turn(turn, "thread_1") == 0
    assert len(await events.read("chat_1", "turns")) == 1
    assert (await events.read("chat_1", "ui"))[-1][1] == {
        "type": "stream.available",
        "turn_id": "turn_1",
        "run_id": "thread_1",
        "generation": 0,
    }
    with pytest.raises(RuntimeError, match="already owned"):
        await thread.register_turn(turn, "thread_2")
