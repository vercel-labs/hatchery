"""Git schedules: static discovery, then durable reconciliation and execution with real
Git, Rotor tickers, and the real SDK. Ported from agentmesh
`tests/unit/test_serve_scheduling.py` and the schedule scenario of
`tests/integration/test_gateway.py`.
"""

import ai

from hatchery.serve import scheduling
from hatchery.store import chats, events
from hatchery.workspace import files

from tests.serve import conftest

AGENT = conftest.AGENT


def source(text: str) -> files.File:
    return files.File(text.encode())


def test_discovers_literal_cron_and_interval_jobs_without_execution() -> None:
    jobs = scheduling.discover_jobs(
        {
            "self/schedules/cleanup/job.py": source(
                '"""Remove stale cache entries."""\n'
                "raise RuntimeError('must not execute')\n"
                'SCHEDULE = {"cron": "0 8 * * *", "tz": "America/New_York"}\n'
                "async def run(job): pass\n"
            ),
            "self/schedules/report/job.py": source('SCHEDULE = {"every": "4h"}\ndef run(job): pass\n'),
            "self/schedules/helpers.py": source("raise RuntimeError('ignored')\n"),
        }
    )

    cleanup, report = jobs
    assert cleanup.name == "cleanup"
    assert cleanup.description == "Remove stale cache entries."
    assert cleanup.kind == "cron" and cleanup.value == "0 8 * * *"
    assert cleanup.timezone == "America/New_York" and cleanup.available
    assert report.kind == "every" and report.value == "4h" and report.available
    assert cleanup.digest != report.digest


def test_commented_none_disabled_and_invalid_jobs_are_handled_independently() -> None:
    jobs = scheduling.discover_jobs(
        {
            "self/schedules/commented/job.py": source("def run(job): pass\n"),
            "self/schedules/none/job.py": source("SCHEDULE = None\ndef run(job): pass\n"),
            "self/schedules/disabled/job.py": source(
                'SCHEDULE = {"every": "2h", "enabled": False}\ndef run(job): pass\n'
            ),
            "self/schedules/bad-cron/job.py": source('SCHEDULE = {"cron": "never"}\ndef run(job): pass\n'),
            "self/schedules/missing-run/job.py": source('SCHEDULE = {"every": "1h"}\n'),
            "self/schedules/too-fast/job.py": source('SCHEDULE = {"every": "30s"}\ndef run(job): pass\n'),
        }
    )

    assert [job.name for job in jobs] == ["bad-cron", "disabled", "missing-run", "too-fast"]
    bad, disabled, missing, too_fast = jobs
    assert bad.error is not None and "5 fields" in bad.error
    assert disabled.available and disabled.enabled is False
    assert missing.error == "job defines no top-level run function"
    assert too_fast.error == "SCHEDULE every must be at least 1m"


def test_dynamic_metadata_and_timezone_on_interval_are_unavailable() -> None:
    jobs = scheduling.discover_jobs(
        {
            "self/schedules/dynamic/job.py": source('SCHEDULE = dict(every="1h")\ndef run(job): pass\n'),
            "self/schedules/zoned/job.py": source(
                'SCHEDULE = {"every": "1h", "tz": "UTC"}\ndef run(job): pass\n'
            ),
        }
    )

    assert jobs[0].error is not None
    assert jobs[1].error == "SCHEDULE tz is only valid with cron"


async def test_main_schedule_reconciles_executes_pauses_and_is_cancelled_on_removal(
    serve: conftest.Serve,
) -> None:
    def job(every: str) -> files.File:
        return source(
            '"""Clean persisted state."""\n'
            f'SCHEDULE = {{"every": "{every}"}}\n'
            "def run(job):\n"
            "    return {'cleaned': True, 'revision': job.revision}\n"
        )

    async with serve() as app:
        revision = await app.merge_tree(AGENT, {"self/schedules/cleanup/job.py": job("1h")}, "add-job")
        await app.rt.drain()

        listed = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()
        assert listed["revision"] == listed["reconciled_revision"] == revision
        assert listed["schedules"][0] == {
            "name": "cleanup",
            "description": "Clean persisted state.",
            "kind": "every",
            "value": "1h",
            "timezone": None,
            "enabled": True,
            "paused": False,
            "available": True,
            "error": None,
            "next_at": listed["schedules"][0]["next_at"],
            "running": False,
            "last_run": None,
        }
        assert listed["schedules"][0]["next_at"] is not None
        initial, _ = await app.rt.client.query(
            scheduling.scheduler_id(AGENT), scheduling.Scheduler.status
        )
        initial_ticker = initial["jobs"]["cleanup"]["ticker"]

        # Operator pause removes the timer without editing Git.
        paused = await app.app.post(
            f"/api/agents/{AGENT}/schedules/cleanup/pause", json={"request_id": "pause-cleanup"}
        )
        assert paused.status_code == 202 and paused.json()["paused"] is True
        await app.rt.drain()
        schedule = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()["schedules"][0]
        assert schedule["paused"] is True and schedule["next_at"] is None
        await app.rt.advance("1h")
        await app.rt.drain()
        schedule = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()["schedules"][0]
        assert schedule["last_run"] is None

        resumed = await app.app.post(
            f"/api/agents/{AGENT}/schedules/cleanup/resume", json={"request_id": "resume-cleanup"}
        )
        assert resumed.status_code == 202 and resumed.json()["paused"] is False
        await app.rt.drain()
        schedule = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()["schedules"][0]
        assert schedule["paused"] is False and schedule["next_at"] is not None

        # The occurrence runs in the serve sandbox at the armed revision.
        await app.rt.advance("1h")
        await app.rt.drain()
        schedule = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()["schedules"][0]
        assert schedule["last_run"]["status"] == 200
        assert schedule["last_run"]["result"] == {"cleaned": True, "revision": revision}
        assert len(app.sandboxes.serve_sandboxes()) == 1

        # A pause survives a rule edit, which replaces the ticker.
        assert (
            await app.app.post(
                f"/api/agents/{AGENT}/schedules/cleanup/pause",
                json={"request_id": "pause-before-rule-change"},
            )
        ).status_code == 202
        await app.rt.drain()
        changed_revision = await app.merge_tree(
            AGENT, {"self/schedules/cleanup/job.py": job("2h")}, "change-job"
        )
        await app.rt.drain()
        changed, _ = await app.rt.client.query(
            scheduling.scheduler_id(AGENT), scheduling.Scheduler.status
        )
        assert changed["revision"] == changed_revision
        assert changed["jobs"]["cleanup"]["ticker"] != initial_ticker
        assert changed["jobs"]["cleanup"]["paused"] is True
        schedule = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()["schedules"][0]
        assert schedule["paused"] is True and schedule["next_at"] is None

        removed_revision = await app.merge_tree(
            AGENT, {"self/schedules/cleanup/job.py": None}, "remove-job"
        )
        await app.rt.drain()
        removed = (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()
        assert removed["revision"] == removed["reconciled_revision"] == removed_revision
        assert removed["schedules"] == []

        retired = await app.app.post(
            f"/api/agents/{AGENT}/retire", json={"request_id": "retire-schedules"}
        )
        assert retired.status_code == 202
        snapshot = await app.rt.client.snapshot(scheduling.scheduler_id(AGENT))
        assert snapshot.process_type == scheduling.Scheduler.__name__
        assert snapshot.terminal_status == "cancelled"


async def test_job_prompts_land_as_turns_and_failures_prompt_at_most_hourly(
    serve: conftest.Serve,
) -> None:
    failure = "Scheduled job `boom` failed: "
    script = [
        ai.user_message("tick done"),
        ai.assistant_message("Recorded the tick."),
    ]
    failure_script = [ai.user_message(failure), ai.assistant_message("I will look at boom.")]
    async with serve(script, failure_script) as app:
        await app.merge_tree(
            AGENT,
            {
                "self/schedules/tick/job.py": source(
                    'SCHEDULE = {"every": "1h"}\n'
                    "from hatchery.sdk import prompt\n"
                    "def run(job):\n"
                    "    prompt('tick done', key=f'tick:{job.id}')\n"
                ),
                "self/schedules/boom/job.py": source(
                    'SCHEDULE = {"every": "30m"}\n'
                    "from hatchery.sdk import Response\n"
                    "def run(job):\n"
                    "    return Response('down', status=503)\n"
                ),
            },
            "add-jobs",
        )
        await app.rt.drain()
        await app.rt.advance("30m")
        await app.rt.drain()
        await app.rt.advance("30m")
        await app.rt.drain()

        listed = {
            s["name"]: s for s in (await app.app.get(f"/api/agents/{AGENT}/schedules")).json()["schedules"]
        }
        assert listed["boom"]["last_run"]["status"] == 503
        assert listed["tick"]["last_run"]["status"] == 204
        found = {c.title: c for c in await chats.list_all() if c.trigger == "schedule"}
        assert set(found) == {"schedule: schedule:tick", "schedule: schedule:boom"}
        tick = [m["parts"][0]["text"] for _, m in await events.read(found["schedule: schedule:tick"].id, "messages")]
        assert tick == ["tick done", "Recorded the tick."]
        boom = [m["parts"][0]["text"] for _, m in await events.read(found["schedule: schedule:boom"].id, "messages")]
        # Two failures, 30 minutes apart, prompt the agent once.
        assert boom == [failure, "I will look at boom."]
        assert not app.model.unused
