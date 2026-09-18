"""Static discovery for agent workspace schedules."""

import ast
import dataclasses
import hashlib
import json
import re
import zoneinfo
from collections import abc

import croniter
import rotor.errors
import rotor.patterns


MAX_NAME_LENGTH = 64
MAX_SOURCE_BYTES = 256 * 1024
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PATH = re.compile(r"agents/([^/]+)/schedules/([^/]+)/job\.py\Z")
_ALLOWED_KEYS = {"cron", "every", "tz", "enabled"}


@dataclasses.dataclass(frozen=True)
class Job:
    """Schedule metadata obtained without importing agent code."""

    agent: str
    name: str
    handler_path: str
    description: str | None
    kind: str | None = None
    value: str | None = None
    timezone: str | None = None
    enabled: bool = True
    prompt: str | None = None
    digest: str = ""
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None

    @property
    def rule(self) -> str | None:
        if self.kind is None or self.value is None:
            return None
        return f"{self.kind}={self.value}"


@dataclasses.dataclass(frozen=True)
class JobTable:
    jobs: tuple[Job, ...]


def discover_jobs(tree: abc.Mapping[str, object]) -> JobTable:
    """Discover direct ``agents/<slug>/schedules/<name>/job.py`` files using AST only.

    Mapping values may be UTF-8 ``str``/``bytes`` or file records whose ``content``
    attribute contains either form. Candidate source is never imported or executed.
    """
    jobs: list[Job] = []
    for handler_path in sorted(tree):
        match = _PATH.fullmatch(handler_path)
        if match is None:
            continue
        agent, name = match.groups()
        description: str | None = None
        kind: str | None = None
        value: str | None = None
        timezone: str | None = None
        enabled = True
        prompt: str | None = None
        error: str | None = None
        try:
            if not _NAME.fullmatch(agent):
                raise ValueError(f"invalid agent slug {agent!r}")
            if not _NAME.fullmatch(name):
                raise ValueError(f"invalid schedule name {name!r}")

            file = tree[handler_path]
            content = file if isinstance(file, (str, bytes)) else getattr(file, "content", None)
            if not isinstance(content, (str, bytes)):
                raise ValueError("job source must be UTF-8 text or bytes")
            encoded_source = content.encode() if isinstance(content, str) else content
            if len(encoded_source) > MAX_SOURCE_BYTES:
                raise ValueError("job source exceeds 256 KiB")
            module = ast.parse(encoded_source.decode("utf-8"), filename=handler_path)
            description = ast.get_docstring(module, clean=True)
            run = next(
                (
                    node
                    for node in module.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "run"
                ),
                None,
            )
            declarations: list[ast.expr | None] = []
            prompt_declarations: list[ast.expr | None] = []
            for node in module.body:
                if isinstance(node, ast.Assign):
                    if any(
                        isinstance(target, ast.Name) and target.id == "SCHEDULE"
                        for target in node.targets
                    ):
                        declarations.append(node.value)
                    if any(
                        isinstance(target, ast.Name) and target.id == "PROMPT"
                        for target in node.targets
                    ):
                        prompt_declarations.append(node.value)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if node.target.id == "SCHEDULE":
                        declarations.append(node.value)
                    if node.target.id == "PROMPT":
                        prompt_declarations.append(node.value)
            if not declarations:
                continue
            if len(declarations) != 1:
                raise ValueError("job defines multiple top-level SCHEDULE declarations")
            try:
                schedule = ast.literal_eval(declarations[0])
            except (ValueError, TypeError, MemoryError, RecursionError) as exc:
                raise ValueError("SCHEDULE must be a literal dict or None") from exc
            if schedule is None:
                continue
            if type(schedule) is not dict:
                raise ValueError("SCHEDULE must be a literal dict or None")
            if any(type(key) is not str for key in schedule):
                raise ValueError("SCHEDULE keys must be strings")
            unknown = set(schedule) - _ALLOWED_KEYS
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
                value = " ".join(value.split())
                if len(value.split()) != 5 or not croniter.croniter.is_valid(
                    value, second_at_beginning=False
                ):
                    raise ValueError("SCHEDULE cron must be a valid five-field expression")
                if timezone is not None:
                    try:
                        zoneinfo.ZoneInfo(timezone)
                    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
                        raise ValueError(f"unknown SCHEDULE timezone {timezone!r}") from exc
            else:
                if timezone is not None:
                    raise ValueError("SCHEDULE tz is only valid with cron")
                if rotor.patterns.Interval(value).seconds() < 60:
                    raise ValueError("SCHEDULE every must be at least 1m")
            if run is None:
                raise ValueError("job defines no top-level run function")
            if len(prompt_declarations) != 1:
                raise ValueError("job must define one literal top-level PROMPT")
            try:
                raw_prompt = ast.literal_eval(prompt_declarations[0])
            except (ValueError, TypeError, MemoryError, RecursionError) as exc:
                raise ValueError("PROMPT must be a literal string") from exc
            if not isinstance(raw_prompt, str) or not raw_prompt.strip():
                raise ValueError("PROMPT must be a non-empty literal string")
            prompt = raw_prompt.strip()
        except UnicodeDecodeError:
            error = "job source is not UTF-8"
        except SyntaxError as exc:
            location = f" at line {exc.lineno}" if exc.lineno is not None else ""
            error = f"syntax error{location}: {exc.msg}"
        except (rotor.errors.ConfigurationError, TypeError, ValueError) as exc:
            error = str(exc)

        digest = ""
        if error is None and kind is not None and value is not None:
            digest_input = json.dumps(
                [handler_path, kind, value, timezone, prompt], separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(digest_input).hexdigest()[:16]
        jobs.append(
            Job(
                agent=agent,
                name=name,
                handler_path=handler_path,
                description=description,
                kind=kind,
                value=value,
                timezone=timezone,
                enabled=enabled,
                prompt=prompt,
                digest=digest,
                error=error,
            )
        )
    return JobTable(tuple(jobs))
