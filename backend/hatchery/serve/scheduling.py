"""Git-declared schedules: static job discovery and durable Rotor reconciliation.

Ported from agentmesh `serve/scheduling.py`. `self/schedules/<name>/job.py` declares
a literal `SCHEDULE` (exactly one of `cron` with optional `tz`, or `every`) and a
top-level `run(job)`; files are parsed, never imported. Each agent has one keyed
`Scheduler` with one generation-fenced Rotor `Ticker` child per enabled valid job.
Operator pause is durable and survives rule edits until resumed. Occurrences run as
`execute_scheduled_job` tasks in the agent's serve sandbox. Main observations and the
five-minute maintenance heartbeat (`/api/cron`) converge on `ScheduleControl.reconcile`.

Hatchery prompt jobs (`store/jobs.py`) are a separate job kind and are not touched here.
"""

import ast
import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import typing
from typing import Any

import rotor
import rotor.errors
import rotor.patterns

from hatchery import environment, vault
from hatchery.agent import supervisor
from hatchery.workspace import files

log = logging.getLogger(__name__)
SCOPE = supervisor.SCOPE
_JOB_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SOURCE_LIMIT = 256 * 1024
_RUN_HISTORY = 20


@dataclasses.dataclass(frozen=True)
class Job:
    """Schedule metadata obtained without importing agent code."""

    name: str
    handler_path: str
    description: str | None
    kind: str | None = None
    value: str | None = None
    timezone: str | None = None
    enabled: bool = True
    digest: str = ""
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None


def discover_jobs(tree: files.Tree) -> tuple[Job, ...]:
    """Discover direct `self/schedules/<name>/job.py` files using AST only."""
    jobs: list[Job] = []
    for handler_path in sorted(tree):
        match = re.fullmatch(r"self/schedules/([^/]+)/job\.py", handler_path)
        if match is None:
            continue
        name = match.group(1)
        description: str | None = None
        kind: str | None = None
        value: str | None = None
        timezone: str | None = None
        enabled = True
        error: str | None = None
        try:
            if not _JOB_NAME.fullmatch(name):
                raise ValueError(f"invalid job name {name!r}")
            content = tree[handler_path].content
            if len(content) > _SOURCE_LIMIT:
                raise ValueError("job source exceeds 256 KiB")
            module = ast.parse(content.decode("utf-8"), filename=handler_path)
            description = ast.get_docstring(module, clean=True)
            has_run = any(
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "run"
                for node in module.body
            )
            schedule_node: ast.expr | None = None
            for node in module.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "SCHEDULE"
                    for target in node.targets
                ):
                    schedule_node = node.value
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "SCHEDULE"
                ):
                    schedule_node = node.value
            if schedule_node is None:
                continue
            schedule = ast.literal_eval(schedule_node)
            if schedule is None:
                continue
            if type(schedule) is not dict:
                raise ValueError("SCHEDULE must be a literal dict or None")
            if any(type(key) is not str for key in schedule):
                raise ValueError("SCHEDULE keys must be strings")
            unknown = set(schedule) - {"cron", "every", "tz", "enabled"}
            if unknown:
                raise ValueError(f"unknown SCHEDULE field {sorted(unknown)[0]!r}")
            enabled = schedule.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError("SCHEDULE enabled must be a boolean")
            choices = [key for key in ("cron", "every") if key in schedule]
            if len(choices) != 1:
                raise ValueError("SCHEDULE must define exactly one of cron or every")
            kind = choices[0]
            raw_value = schedule[kind]
            if type(raw_value) is not str or not raw_value.strip():
                raise ValueError(f"SCHEDULE {kind} must be a non-empty string")
            value = raw_value.strip()
            raw_timezone = schedule.get("tz")
            if raw_timezone is not None and (
                type(raw_timezone) is not str or not raw_timezone.strip()
            ):
                raise ValueError("SCHEDULE tz must be a non-empty string")
            timezone = raw_timezone.strip() if isinstance(raw_timezone, str) else None
            if kind == "cron":
                rotor.patterns.Cron(value, tz=timezone).compile()
            else:
                if timezone is not None:
                    raise ValueError("SCHEDULE tz is only valid with cron")
                if rotor.patterns.Interval(value).seconds() < 60:
                    raise ValueError("SCHEDULE every must be at least 1m")
            if not has_run:
                raise ValueError("job defines no top-level run function")
        except UnicodeDecodeError:
            error = "job source is not UTF-8"
        except SyntaxError as exc:
            location = f" at line {exc.lineno}" if exc.lineno is not None else ""
            error = f"syntax error{location}: {exc.msg}"
        except (rotor.errors.ConfigurationError, TypeError, ValueError) as exc:
            error = str(exc)

        digest = ""
        if error is None and kind is not None and value is not None:
            encoded = json.dumps(
                [handler_path, kind, value, timezone], separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(encoded).hexdigest()[:16]
        jobs.append(
            Job(name, handler_path, description, kind, value, timezone, enabled, digest, error)
        )
    return tuple(jobs)


@rotor.message
class ScheduleSpec:
    name: str
    handler_path: str
    kind: str
    value: str
    timezone: str | None
    digest: str


@rotor.message
class ReconcileSchedules:
    revision: str
    schedules: list[ScheduleSpec]


@rotor.message
class RecordScheduleAgents:
    revision: str
    agents: list[str]


@rotor.message
class PauseSchedule:
    name: str


@rotor.message
class ResumeSchedule:
    name: str


@rotor.message
class ScheduledJobDue:
    name: str
    generation: int
    scheduled_for: float | None = None


@rotor.state
class ScheduledJobState:
    spec: ScheduleSpec = dataclasses.field(
        default_factory=lambda: ScheduleSpec("", "", "", "", None, "")
    )
    generation: int = 0
    ticker: rotor.ProcessRef | None = None


@rotor.state
class SchedulerState:
    agent_id: str = ""
    revision: str = ""
    generation: int = 0
    jobs: dict[str, ScheduledJobState] = dataclasses.field(default_factory=dict)
    running: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    history: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    last_failure_prompt: dict[str, float] = dataclasses.field(default_factory=dict)
    paused: list[str] = dataclasses.field(default_factory=list)


@rotor.state
class ScheduleRegistryState:
    revision: str = ""
    agents: list[str] = dataclasses.field(default_factory=list)


def _retry(error: Exception, attempt: int) -> str | None:
    return f"{2**attempt}s" if attempt < 4 else None


@rotor.patterns.task(retry=_retry)
async def execute_scheduled_job(
    agent_id: str,
    revision: str,
    name: str,
    handler_path: str,
    scheduled_for: float,
    run_id: str,
) -> dict[str, Any]:
    """Execute one occurrence through the worker-side serve runtime."""
    from hatchery.serve import service  # service imports this module

    return await service.current().invoke_job(
        agent_id,
        revision=revision,
        name=name,
        handler_path=handler_path,
        scheduled_for=scheduled_for,
        run_id=run_id,
    )


execute_scheduled_job.handle_timeout = "280s"
execute_scheduled_job.retention = "1d"


@rotor.patterns.task(retry=_retry)
async def prompt_agent(agent_id: str, text: str, key: str, thread: str) -> str:
    """Land a schedule failure prompt as a normal turn of the agent."""
    return await supervisor.prompt(agent_id, text, key=key, thread=thread, source="schedule")


class ScheduleRegistry(rotor.DurableProcess[ScheduleRegistryState]):
    """Durably retain agents so removed directories cannot leave stale Tickers."""

    @rotor.on
    async def start(self, msg: rotor.Start) -> None:
        pass

    @rotor.on
    async def record_agents(self, msg: RecordScheduleAgents) -> None:
        self.state.revision = msg.revision
        self.state.agents = list(msg.agents)

    @rotor.query
    def status(self) -> dict[str, Any]:
        return {"revision": self.state.revision, "agents": list(self.state.agents)}


class Scheduler(rotor.DurableProcess[SchedulerState]):
    """One agent's durable schedule registry and Ticker supervisor."""

    @rotor.on
    async def start(self, msg: rotor.Start) -> None:
        self.state.agent_id = str(msg.input["agent_id"])

    @rotor.on
    async def reconcile(self, msg: ReconcileSchedules) -> None:
        if msg.revision == self.state.revision:
            return
        desired = {spec.name: spec for spec in msg.schedules}
        for name, current in list(self.state.jobs.items()):
            wanted = desired.get(name)
            if wanted is None or wanted.digest != current.spec.digest:
                if current.ticker is not None:
                    self.cancel(current.ticker)
                self.state.jobs.pop(name, None)
                self.state.last_failure_prompt.pop(name, None)
                for run_id, running in list(self.state.running.items()):
                    if running.get("name") == name:
                        self.cancel(str(running["ref"]))
                        self.state.running.pop(run_id, None)
        for name, spec in desired.items():
            if name in self.state.jobs and self.state.jobs[name].ticker is not None:
                continue
            self._arm(spec)
        self.state.revision = msg.revision
        rotor.record(
            "schedules_reconciled",
            {"agent_id": self.state.agent_id, "revision": msg.revision, "count": len(desired)},
        )

    @rotor.on
    async def due(self, msg: ScheduledJobDue) -> None:
        current = self.state.jobs.get(msg.name)
        if (
            current is None
            or current.generation != msg.generation
            or msg.scheduled_for is None
            or msg.name in self.state.paused
        ):
            return
        if any(running.get("name") == msg.name for running in self.state.running.values()):
            rotor.record("scheduled_job_overlap_skipped", {"name": msg.name})
            return
        run_id = hashlib.sha256(
            f"{self.state.agent_id}:{msg.name}:{msg.scheduled_for.hex()}".encode()
        ).hexdigest()
        ref = self.spawn(
            execute_scheduled_job,
            input={
                "agent_id": self.state.agent_id,
                "revision": self.state.revision,
                "name": msg.name,
                "handler_path": current.spec.handler_path,
                "scheduled_for": msg.scheduled_for,
                "run_id": run_id,
            },
            key=f"run:{run_id}",
        )
        self.state.running[run_id] = {
            "ref": ref.id,
            "name": msg.name,
            "scheduled_for": msg.scheduled_for,
            "started_at": self.now(),
            "revision": self.state.revision,
        }

    @rotor.on
    async def pause(self, msg: PauseSchedule) -> None:
        current = self.state.jobs.get(msg.name)
        if current is None or msg.name in self.state.paused:
            return
        self.state.paused.append(msg.name)
        if current.ticker is not None:
            self.send(current.ticker, rotor.patterns.Pause())
        rotor.record("schedule_paused", {"name": msg.name})

    @rotor.on
    async def resume(self, msg: ResumeSchedule) -> None:
        current = self.state.jobs.get(msg.name)
        if current is None or msg.name not in self.state.paused:
            return
        self.state.paused.remove(msg.name)
        if current.ticker is not None:
            self.send(current.ticker, rotor.patterns.Resume())
        rotor.record("schedule_resumed", {"name": msg.name})

    @rotor.on(execute_scheduled_job.Done)
    async def job_done(self, msg: rotor.ChildDone) -> None:
        run_id = msg.key.removeprefix("run:")
        running = self.state.running.pop(run_id, None)
        if running is None:
            return
        output = msg.output if isinstance(msg.output, dict) else {}
        self._finish(running, output)
        if int(output.get("status", 500)) >= 500:
            self._prompt_failure(running["name"], run_id, str(output.get("error", "job failed")))

    @rotor.on(execute_scheduled_job.Failed)
    async def job_failed(self, msg: rotor.ChildFailed) -> None:
        run_id = msg.key.removeprefix("run:")
        running = self.state.running.pop(run_id, None)
        if running is None:
            return
        detail = str(msg.reason)
        self._finish(running, {"status": 500, "error": detail})
        self._prompt_failure(running["name"], run_id, detail)

    @rotor.on(rotor.patterns.Ticker.Failed)
    async def ticker_failed(self, msg: rotor.ChildFailed) -> None:
        current = next((job for job in self.state.jobs.values() if job.ticker == msg.ref), None)
        if current is None:
            return
        self.state.jobs.pop(current.spec.name, None)
        if isinstance(msg.reason, rotor.Cancelled):
            return
        rotor.record("schedule_ticker_failed", {"key": msg.key, "reason": str(msg.reason)})
        self._arm(current.spec)

    @rotor.on(prompt_agent.Done)
    async def prompt_done(self, msg: rotor.ChildDone) -> None:
        pass

    @rotor.on(prompt_agent.Failed)
    async def prompt_failed(self, msg: rotor.ChildFailed) -> None:
        rotor.record("schedule_prompt_failed", {"key": msg.key, "reason": str(msg.reason)})

    def _arm(self, spec: ScheduleSpec) -> None:
        self.state.generation += 1
        generation = self.state.generation
        rule = (
            rotor.patterns.Cron(spec.value, tz=spec.timezone)
            if spec.kind == "cron"
            else rotor.patterns.Interval(spec.value)
        )
        ticker = self.spawn(
            rotor.patterns.Ticker,
            rotor.patterns.Repeat(rule, ScheduledJobDue(spec.name, generation)),
            key=f"ticker:{spec.name}:{generation}",
        )
        self.state.jobs[spec.name] = ScheduledJobState(spec, generation, ticker)
        if spec.name in self.state.paused:
            self.send(ticker, rotor.patterns.Pause())

    def _finish(self, running: dict[str, Any], output: dict[str, Any]) -> None:
        self.state.history.append(
            {
                **{key: value for key, value in running.items() if key != "ref"},
                "finished_at": self.now(),
                "status": int(output.get("status", 500)),
                "result": output.get("result"),
                "error": str(output.get("error", ""))[:2000],
            }
        )
        self.state.history = self.state.history[-_RUN_HISTORY:]

    def _prompt_failure(self, name: str, run_id: str, detail: str) -> None:
        """Wake the agent about a failed job at most once per hour per job."""
        last = self.state.last_failure_prompt.get(name, 0)
        if self.now() - last < 3600:
            return
        self.state.last_failure_prompt[name] = self.now()
        self.spawn(
            prompt_agent,
            input={
                "agent_id": self.state.agent_id,
                "text": f"Scheduled job `{name}` failed: {detail[:1000]}",
                "key": f"schedule-failure:{run_id}",
                "thread": f"schedule:{name}",
            },
            key=f"failure:{run_id}",
        )

    @rotor.query
    def status(self) -> dict[str, Any]:
        return {
            "agent_id": self.state.agent_id,
            "revision": self.state.revision,
            "jobs": {
                name: {
                    "ticker": job.ticker,
                    "generation": job.generation,
                    "digest": job.spec.digest,
                    "paused": name in self.state.paused,
                }
                for name, job in self.state.jobs.items()
            },
            "running": list(self.state.running.values()),
            "history": list(self.state.history),
        }


PROCESSES: list[typing.Any] = [
    vault.SecretVault,
    Scheduler,
    ScheduleRegistry,
    rotor.patterns.Ticker,
    execute_scheduled_job,
    prompt_agent,
]


def scheduler_id(agent_id: str) -> str:
    return rotor.singleton_id(Scheduler, agent_id, scope=SCOPE)


class ScheduleControl:
    """Observe Git main and idempotently reconcile every agent's Scheduler.

    Agentmesh `ScheduleControlService`. Git is authoritative; the process-local
    revision only skips repeated observations of one revision.
    """

    def __init__(self, env: environment.Environment) -> None:
        self.env = env
        self._lock = asyncio.Lock()
        self._revision = ""

    async def observe(self, revision: str) -> None:
        if revision != self._revision:
            await self.reconcile()

    async def reconcile(self) -> str:
        from hatchery.agent import runtime
        from hatchery.serve import service  # service imports this module

        client = runtime.client
        async with self._lock:
            revision, trees = await self.env.workspaces.read_schedule_main()
            if revision == self._revision:
                return revision
            service.current().main_advanced(revision)
            registry = await client.start(ScheduleRegistry, key="schedules", scope=SCOPE)
            prior, _ = await client.query(registry.id, ScheduleRegistry.status)
            active: list[str] = []
            for agent_id, tree in trees.items():
                if await vault.retired(agent_id):
                    continue
                active.append(agent_id)
                schedules = [
                    ScheduleSpec(
                        job.name,
                        job.handler_path,
                        str(job.kind),
                        str(job.value),
                        job.timezone,
                        job.digest,
                    )
                    for job in discover_jobs(tree)
                    if job.available and job.enabled
                ]
                handle = await client.start(
                    Scheduler, input={"agent_id": agent_id}, key=agent_id, scope=SCOPE
                )
                await handle.send(
                    ReconcileSchedules(revision, schedules),
                    idempotency_key=f"schedules:{agent_id}:{revision}",
                )
            for agent_id in set(prior["agents"]) - set(active):
                try:
                    await client.send(
                        scheduler_id(str(agent_id)),
                        ReconcileSchedules(revision, []),
                        idempotency_key=f"schedules:{agent_id}:{revision}",
                    )
                except (rotor.MailboxClosed, rotor.ProcessNotFound):
                    pass
            await registry.send(
                RecordScheduleAgents(revision, active),
                idempotency_key=f"schedule-agents:{revision}",
            )
            self._revision = revision
            return revision

    async def retire(self, agent_id: str) -> None:
        from hatchery.agent import runtime

        try:
            await runtime.client.cancel(scheduler_id(agent_id))
        except rotor.ProcessNotFound:
            pass

    async def set_paused(self, agent_id: str, name: str, paused: bool, request_id: str) -> str:
        from hatchery.agent import runtime

        try:
            status, _ = await runtime.client.query(scheduler_id(agent_id), Scheduler.status)
        except rotor.ProcessNotFound as error:
            raise KeyError(name) from error
        if name not in status["jobs"]:
            raise KeyError(name)
        return await runtime.client.send(
            scheduler_id(agent_id),
            PauseSchedule(name) if paused else ResumeSchedule(name),
            idempotency_key=f"schedule:{'pause' if paused else 'resume'}:{request_id}",
        )


def current() -> ScheduleControl:
    """The schedule control of the installed Environment (process-local)."""
    env = environment.Environment.current()
    if env.services.schedules is None:
        env.services.schedules = ScheduleControl(env)
    return typing.cast(ScheduleControl, env.services.schedules)


async def observe(revision: str) -> None:
    """The workspace repo's main observer: reconcile when main moved."""
    await current().observe(revision)
