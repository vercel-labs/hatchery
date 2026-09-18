import dataclasses

from agent import schedules


def test_discovers_direct_jobs_without_executing_source():
    source = '''"""Send the morning report."""
SCHEDULE = {"cron": "0  9 * * 1-5", "tz": "America/New_York"}
PROMPT = "Send the morning report."

def run(job):
    raise RuntimeError("discovery must not execute this")
'''

    result = schedules.discover_jobs(
        {
            "agents/reporter/schedules/morning/job.py": source,
            "agents/reporter/schedules/morning/helper.py": "not Python",
            "agents/reporter/schedules/morning/nested/job.py": "not Python",
            "other/reporter/schedules/ignored/job.py": "not Python",
        }
    )

    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job == schedules.Job(
        agent="reporter",
        name="morning",
        handler_path="agents/reporter/schedules/morning/job.py",
        description="Send the morning report.",
        kind="cron",
        value="0 9 * * 1-5",
        timezone="America/New_York",
        enabled=True,
        prompt="Send the morning report.",
        digest=job.digest,
        error=None,
    )
    assert len(job.digest) == 16
    assert job.available is True
    assert job.rule == "cron=0 9 * * 1-5"


def test_discovers_interval_and_disabled_jobs_from_file_records():
    @dataclasses.dataclass
    class File:
        content: bytes

    result = schedules.discover_jobs(
        {
            "agents/ops_bot/schedules/cache-clean/job.py": File(
                b'SCHEDULE = {"every": "15m", "enabled": False}\n'
                b'PROMPT = "Clean the cache."\n'
                b"async def run(job):\n    return None\n"
            )
        }
    )

    job = result.jobs[0]
    assert (job.kind, job.value, job.timezone, job.enabled, job.error) == (
        "every",
        "15m",
        None,
        False,
        None,
    )


def test_missing_or_none_declaration_is_not_discovered():
    result = schedules.discover_jobs(
        {
            "agents/ops/schedules/missing/job.py": "def run(job): pass\n",
            "agents/ops/schedules/retired/job.py": "SCHEDULE = None\n",
        }
    )

    assert result == schedules.JobTable(())


def test_rejects_non_literal_schedule_without_evaluating_it():
    result = schedules.discover_jobs(
        {
            "agents/ops/schedules/danger/job.py": (
                "def make_schedule():\n    raise RuntimeError('must not run')\n"
                "SCHEDULE = make_schedule()\n"
                "def run(job):\n    pass\n"
            )
        }
    )

    assert result.jobs[0].error == "SCHEDULE must be a literal dict or None"
    assert result.jobs[0].digest == ""
    assert result.jobs[0].available is False


def test_reports_metadata_syntax_and_entrypoint_errors_clearly():
    result = schedules.discover_jobs(
        {
            "agents/ops/schedules/bad-cron/job.py": (
                'SCHEDULE = {"cron": "once daily"}\ndef run(job):\n    pass\n'
            ),
            "agents/ops/schedules/bad-every/job.py": (
                'SCHEDULE = {"every": "30s", "tz": "UTC"}\ndef run(job):\n    pass\n'
            ),
            "agents/ops/schedules/both/job.py": (
                'SCHEDULE = {"cron": "0 * * * *", "every": "1h"}\n'
                "def run(job):\n    pass\n"
            ),
            "agents/ops/schedules/missing-run/job.py": 'SCHEDULE = {"every": "1h"}\n',
            "agents/ops/schedules/syntax/job.py": "SCHEDULE = {\n",
        }
    )

    errors = {job.name: job.error for job in result.jobs}
    assert "valid five-field" in errors["bad-cron"]
    assert errors["bad-every"] == "SCHEDULE tz is only valid with cron"
    assert "exactly one" in errors["both"]
    assert errors["missing-run"] == "job defines no top-level run function"
    assert errors["syntax"].startswith("syntax error at line 1:")


def test_enforces_names_source_size_timezone_and_literal_types():
    oversized = "#" * (schedules.MAX_SOURCE_BYTES + 1)
    result = schedules.discover_jobs(
        {
            "agents/Bad/schedules/job/job.py": 'SCHEDULE = {"every": "1h"}\ndef run(job): pass\n',
            "agents/ops/schedules/Bad/job.py": 'SCHEDULE = {"every": "1h"}\ndef run(job): pass\n',
            "agents/ops/schedules/large/job.py": oversized,
            "agents/ops/schedules/timezone/job.py": (
                'SCHEDULE = {"cron": "0 9 * * *", "tz": "Mars/Olympus"}\n'
                "def run(job): pass\n"
            ),
            "agents/ops/schedules/enabled/job.py": (
                'SCHEDULE = {"every": "1h", "enabled": 1}\ndef run(job): pass\n'
            ),
        }
    )

    errors = [job.error for job in result.jobs]
    assert any(error.startswith("invalid agent slug") for error in errors)
    assert any(error.startswith("invalid schedule name") for error in errors)
    assert "job source exceeds 256 KiB" in errors
    assert any(error.startswith("unknown SCHEDULE timezone") for error in errors)
    assert "SCHEDULE enabled must be a boolean" in errors


def test_digest_tracks_normalized_rule_not_job_body_or_enabled_state():
    path = "agents/ops/schedules/cleanup/job.py"
    first = schedules.discover_jobs(
        {path: 'SCHEDULE = {"every": "1h"}\nPROMPT = "Run cleanup."\ndef run(job): return 1\n'}
    ).jobs[0]
    changed_body = schedules.discover_jobs(
        {path: 'SCHEDULE = {"every": "1h", "enabled": False}\nPROMPT = "Run cleanup."\ndef run(job): return 2\n'}
    ).jobs[0]
    changed_rule = schedules.discover_jobs(
        {path: 'SCHEDULE = {"every": "2h"}\nPROMPT = "Run cleanup."\ndef run(job): return 1\n'}
    ).jobs[0]

    assert first.digest == changed_body.digest
    assert first.digest != changed_rule.digest
