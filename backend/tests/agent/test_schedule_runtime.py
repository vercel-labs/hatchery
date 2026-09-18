import models
from agent import schedule_runtime
from store import agent_files, agents, jobs


async def test_reconciles_git_schedule_into_durable_job(monkeypatch):
    agent = await agents.create("Reporter")
    source = '''"""Send the report."""
SCHEDULE = {"every": "15m"}
PROMPT = "Inspect the reports and summarize failures."

def run(job):
    return job.prompt(PROMPT)
'''

    async def configured():
        return True

    monkeypatch.setattr(agent_files, "configured", configured)

    async def tree(_slug, revision=None):
        return (
            models.AgentFilesSnapshot(
                agent_slug=agent.slug,
                revision="a" * 40,
                files=["schedules/report/job.py"],
            ),
            {"schedules/report/job.py": source},
        )

    monkeypatch.setattr(agent_files, "tree", tree)

    revision = await schedule_runtime.reconcile_agent(agent)
    stored = await jobs.list_for_agent(agent.id, f"agent:{agent.id}")
    status = await schedule_runtime.status(agent)

    assert revision == "a" * 40
    assert len(stored) == 1
    assert stored[0].name == "report"
    assert stored[0].schedule_kind == "every"
    assert stored[0].schedule == "15m"
    assert stored[0].prompt == "Inspect the reports and summarize failures."
    assert status.reconciled_revision == revision
    assert status.schedules[0]["next_run_at"] is not None


async def test_reconcile_removes_retired_schedule(monkeypatch):
    agent = await agents.create("Cleaner")
    created = await jobs.create(
        agent.id,
        f"agent:{agent.id}",
        "1h",
        "clean",
        name="cleanup",
        schedule_kind="every",
        source_digest="old",
    )

    async def configured():
        return True

    monkeypatch.setattr(agent_files, "configured", configured)

    async def tree(_slug, revision=None):
        return (
            models.AgentFilesSnapshot(agent_slug=agent.slug, revision="b" * 40, files=[]),
            {},
        )

    monkeypatch.setattr(agent_files, "tree", tree)

    await schedule_runtime.reconcile_agent(agent)

    assert await jobs.get(created.id) is None
