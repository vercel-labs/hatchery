"""Reconcile Git-backed schedule declarations into Hatchery's durable job outbox."""

import dataclasses
import uuid

import models
from agent import schedules
from store import agent_files, agents, jobs


@dataclasses.dataclass(frozen=True)
class Status:
    revision: str | None
    reconciled_revision: str | None
    schedules: tuple[dict, ...]


def _job_id(agent: models.Agent, name: str) -> str:
    return f"job_{uuid.uuid5(uuid.NAMESPACE_URL, f'{agent.id}:{name}').hex[:12]}"


async def _discovered(agent: models.Agent) -> tuple[str | None, schedules.JobTable]:
    snapshot, files = await agent_files.tree(agent.slug)
    tree = {f"agents/{agent.slug}/{path}": content for path, content in files.items()}
    return snapshot.revision, schedules.discover_jobs(tree)


async def reconcile_agent(agent: models.Agent) -> str | None:
    """Make durable jobs match one agent's valid enabled files on main."""
    if not await agent_files.configured():
        return None
    revision, table = await _discovered(agent)
    owner_id = f"agent:{agent.id}"
    current = {
        item.name: item
        for item in await jobs.list_for_agent(agent.id, owner_id)
        if item.name is not None
    }
    desired = {
        item.name: item
        for item in table.jobs
        if item.available and item.enabled and item.prompt is not None
    }
    for name, stored in current.items():
        declaration = desired.get(name)
        if declaration is not None and stored.source_digest == declaration.digest:
            continue
        await jobs.delete(stored.id)
    for name, declaration in desired.items():
        stored = current.get(name)
        if stored is not None and stored.source_digest == declaration.digest:
            continue
        created = await jobs.create(
            agent.id,
            owner_id,
            declaration.value or "",
            declaration.prompt or "",
            author_display_name=agent.name,
            name=name,
            schedule_kind=declaration.kind or "cron",
            timezone=declaration.timezone,
            source_digest=declaration.digest,
            job_id=_job_id(agent, name),
        )
        if stored is not None and stored.paused:
            await jobs.set_paused(created.id, True)
    return revision


async def reconcile_all() -> None:
    if not await agent_files.configured():
        return
    for agent in await agents.list_all():
        await reconcile_agent(agent)


async def status(agent: models.Agent) -> Status:
    if not await agent_files.configured():
        return Status(None, None, ())
    revision, table = await _discovered(agent)
    runtime = {
        item.name: item
        for item in await jobs.list_for_agent(agent.id, f"agent:{agent.id}")
        if item.name is not None
    }
    values = []
    for declaration in table.jobs:
        stored = runtime.get(declaration.name)
        values.append(
            {
                "name": declaration.name,
                "description": declaration.description,
                "kind": declaration.kind,
                "value": declaration.value,
                "timezone": declaration.timezone,
                "enabled": declaration.enabled,
                "available": declaration.available,
                "error": declaration.error,
                "digest": declaration.digest,
                "paused": stored.paused if stored is not None else False,
                "next_run_at": stored.next_run_at if stored is not None else None,
                "running": False,
            }
        )
    reconciled = revision if all(
        (not value["enabled"] or not value["available"])
        or runtime.get(value["name"], None) is not None
        and runtime[value["name"]].source_digest == value["digest"]
        for value in values
    ) else None
    return Status(revision, reconciled, tuple(values))
