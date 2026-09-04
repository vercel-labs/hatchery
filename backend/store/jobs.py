"""Scheduled jobs and their durable execution outbox."""

import datetime
import json
import threading
import urllib.parse
import uuid

import ai
import croniter
import pydantic

import models
import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_jobs (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    schedule    TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    paused      BOOLEAN NOT NULL DEFAULT FALSE,
    next_run_at TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_jobs_due ON hatchery_jobs (next_run_at) WHERE NOT paused;

CREATE TABLE IF NOT EXISTS hatchery_job_executions (
    job_id        TEXT NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,
    chat_id       TEXT NOT NULL UNIQUE,
    turn_id       TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    run_id        TEXT,
    lease_token   TEXT,
    lease_until   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, scheduled_for)
);
ALTER TABLE hatchery_job_executions ADD COLUMN IF NOT EXISTS lease_token TEXT;
ALTER TABLE hatchery_job_executions ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
"""

_RETENTION = datetime.timedelta(days=30)
_LEASE = datetime.timedelta(minutes=2)

_lock = threading.Lock()
_schema_ready = False


class Execution(pydantic.BaseModel):
    job_id: str
    scheduled_for: datetime.datetime
    chat_id: str
    turn_id: str
    prompt: str
    run_id: str | None = None
    lease_token: str | None = None
    lease_until: datetime.datetime | None = None


def validate_schedule(value: str) -> str:
    value = " ".join(value.split())
    if len(value.split()) != 5 or not croniter.croniter.is_valid(value, second_at_beginning=False):
        raise ValueError("schedule must be a valid five-field UTC cron expression")
    return value


def next_run(schedule: str, after: datetime.datetime) -> datetime.datetime:
    if after.tzinfo is None:
        after = after.replace(tzinfo=datetime.UTC)
    return croniter.croniter(schedule, after, ret_type=datetime.datetime).get_next(datetime.datetime)


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "jobs").mkdir(parents=True, exist_ok=True)


async def create(space_id: str, owner_id: str, schedule: str, prompt: str) -> models.Job:
    now = datetime.datetime.now(datetime.UTC)
    job = models.Job(
        id=f"job_{uuid.uuid4().hex[:12]}",
        space_id=space_id,
        owner_id=owner_id,
        schedule=validate_schedule(schedule),
        prompt=prompt,
        paused=False,
        next_run_at=next_run(schedule, now).isoformat(),
        created_at=now.isoformat(),
    )
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_jobs (id, space_id, owner_id, schedule, prompt, paused, next_run_at, created_at) "
            "VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7)",
            job.id, job.space_id, job.owner_id, job.schedule, job.prompt,
            datetime.datetime.fromisoformat(job.next_run_at), datetime.datetime.fromisoformat(job.created_at),
        )
    else:
        with _lock:
            _write_job(job)
    return job


async def get(job_id: str) -> models.Job | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow("SELECT * FROM hatchery_jobs WHERE id = $1", job_id)
        return _job(row) if row else None
    with _lock:
        return _read_job(job_id)


async def exists_for_space(space_id: str) -> bool:
    if store.use_postgres():
        from store import db

        return bool(
            await (await db.pool()).fetchval(
                "SELECT EXISTS(SELECT 1 FROM hatchery_jobs WHERE space_id = $1)",
                space_id,
            )
        )
    with _lock:
        return any(
            (job := _read_job_path(path)) is not None and job.space_id == space_id
            for path in (store.data_dir() / "jobs").glob("*.json")
        )


async def delete_for_space(space_id: str) -> None:
    """Delete all jobs for a globally deletable space and their pending outbox rows."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            pending = await conn.fetch(
                "DELETE FROM hatchery_job_executions e USING hatchery_jobs j "
                "WHERE e.job_id = j.id AND j.space_id = $1 AND e.run_id IS NULL "
                "RETURNING e.chat_id",
                space_id,
            )
            await _delete_pending_chats(conn, [row["chat_id"] for row in pending])
            await conn.execute("DELETE FROM hatchery_jobs WHERE space_id = $1", space_id)
        return
    with _lock:
        job_ids = {
            job.id
            for path in (store.data_dir() / "jobs").glob("*.json")
            if (job := _read_job_path(path)) is not None and job.space_id == space_id
        }
        for job_id in job_ids:
            _path(job_id).unlink()
            _delete_pending_executions(job_id)


async def list_for_space(space_id: str, owner_id: str) -> list[models.Job]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT * FROM hatchery_jobs WHERE space_id = $1 AND owner_id = $2 ORDER BY created_at",
            space_id, owner_id,
        )
        return [_job(row) for row in rows]
    with _lock:
        found = [_read_job_path(path) for path in (store.data_dir() / "jobs").glob("*.json")]
        return sorted(
            [job for job in found if job and job.space_id == space_id and job.owner_id == owner_id],
            key=lambda job: job.created_at,
        )


async def update(job_id: str, schedule: str, prompt: str) -> models.Job | None:
    schedule = validate_schedule(schedule)
    next_at = next_run(schedule, datetime.datetime.now(datetime.UTC))
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "UPDATE hatchery_jobs SET schedule = $2, prompt = $3, next_run_at = $4 "
            "WHERE id = $1 RETURNING *", job_id, schedule, prompt, next_at,
        )
        return _job(row) if row else None
    with _lock:
        job = _read_job(job_id)
        if job is None:
            return None
        job.schedule = schedule
        job.prompt = prompt
        job.next_run_at = next_at.isoformat()
        _write_job(job)
        return job


async def set_paused(job_id: str, paused: bool) -> models.Job | None:
    next_at = datetime.datetime.now(datetime.UTC)
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE hatchery_jobs SET paused = $2, next_run_at = CASE WHEN $2 THEN next_run_at "
                "ELSE $3 END WHERE id = $1 RETURNING *", job_id, paused, next_at,
            )
            job = _job(row) if row else None
            if paused:
                pending = await conn.fetch(
                    "DELETE FROM hatchery_job_executions WHERE job_id = $1 AND run_id IS NULL "
                    "RETURNING chat_id",
                    job_id,
                )
                await _delete_pending_chats(conn, [row["chat_id"] for row in pending])
            elif job is not None:
                await conn.execute(
                    "UPDATE hatchery_jobs SET next_run_at = $2 WHERE id = $1",
                    job.id, next_run(job.schedule, next_at),
                )
                job.next_run_at = next_run(job.schedule, next_at).isoformat()
        return job
    with _lock:
        job = _read_job(job_id)
        if job is None:
            return None
        job.paused = paused
        if paused:
            _delete_pending_executions(job_id)
        else:
            job.next_run_at = next_run(job.schedule, next_at).isoformat()
        _write_job(job)
        return job


async def delete(job_id: str) -> bool:
    """Delete a job and all executions that have not started."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            result = await conn.execute("DELETE FROM hatchery_jobs WHERE id = $1", job_id)
            pending = await conn.fetch(
                "DELETE FROM hatchery_job_executions WHERE job_id = $1 AND run_id IS NULL "
                "RETURNING chat_id",
                job_id,
            )
            await _delete_pending_chats(conn, [row["chat_id"] for row in pending])
        return result != "DELETE 0"
    with _lock:
        path = _path(job_id)
        if not path.exists():
            return False
        path.unlink()
        _delete_pending_executions(job_id)
        return True


async def claim_due(now: datetime.datetime | None = None) -> list[Execution]:
    """Create one chat per due job and skip all other missed occurrences."""
    now = now or datetime.datetime.now(datetime.UTC)
    if store.use_postgres():
        from store import db

        claimed = []
        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT * FROM hatchery_jobs WHERE NOT paused AND next_run_at <= $1 "
                "ORDER BY next_run_at FOR UPDATE SKIP LOCKED", now,
            )
            for row in rows:
                job = _job(row)
                scheduled_for = row["next_run_at"]
                chat = _chat_for(job)
                execution = Execution(
                    job_id=job.id, scheduled_for=scheduled_for, chat_id=chat.id,
                    turn_id=f"turn_{uuid.uuid4().hex}", prompt=job.prompt,
                )
                inserted = await conn.fetchrow(
                    "INSERT INTO hatchery_job_executions (job_id, scheduled_for, chat_id, turn_id, prompt) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING chat_id",
                    execution.job_id, execution.scheduled_for, execution.chat_id, execution.turn_id,
                    execution.prompt,
                )
                if inserted:
                    await conn.execute(
                        "INSERT INTO hatchery_chats (id, space_id, data) VALUES ($1, $2, $3::jsonb)",
                        chat.id, chat.space_id, chat.model_dump_json(),
                    )
                    prompt = _prompt_message(execution)
                    await conn.execute(
                        "INSERT INTO hatchery_streams (stream_id, ns, tail_index) "
                        "VALUES ($1, 'messages', 1)",
                        chat.id,
                    )
                    await conn.execute(
                        "INSERT INTO hatchery_events (stream_id, ns, idx, data) "
                        "VALUES ($1, 'messages', 0, $2::jsonb)",
                        chat.id, prompt.model_dump_json(),
                    )
                    claimed.append(execution)
                await conn.execute(
                    "UPDATE hatchery_jobs SET next_run_at = $2 WHERE id = $1",
                    job.id, next_run(job.schedule, now),
                )
        return claimed

    with _lock:
        claimed = []
        for path in (store.data_dir() / "jobs").glob("*.json"):
            job = _read_job_path(path)
            if job is None or job.paused or datetime.datetime.fromisoformat(job.next_run_at) > now:
                continue
            scheduled_for = datetime.datetime.fromisoformat(job.next_run_at)
            execution_path = _execution_path(job.id, scheduled_for)
            if not execution_path.exists():
                chat = _chat_for(job)
                from store import chats, events

                chats._write_chat(chat)
                execution = Execution(
                    job_id=job.id, scheduled_for=scheduled_for, chat_id=chat.id,
                    turn_id=f"turn_{uuid.uuid4().hex}", prompt=job.prompt,
                )
                execution_path.write_text(execution.model_dump_json(), encoding="utf-8")
                _write_prompt(execution, events._path(chat.id, "messages"))
                claimed.append(execution)
            job.next_run_at = next_run(job.schedule, now).isoformat()
            _write_job(job)
        return claimed


async def lease_pending(now: datetime.datetime | None = None) -> list[Execution]:
    """Lease runnable outbox rows so one heartbeat prepares and starts each row."""
    now = now or datetime.datetime.now(datetime.UTC)
    token = uuid.uuid4().hex
    until = now + _LEASE
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT e.* FROM hatchery_job_executions e "
                "JOIN hatchery_jobs j ON j.id = e.job_id "
                "WHERE e.run_id IS NULL AND (e.lease_until IS NULL OR e.lease_until <= $1) "
                "ORDER BY e.created_at FOR UPDATE OF e SKIP LOCKED",
                now,
            )
            for row in rows:
                await conn.execute(
                    "UPDATE hatchery_job_executions SET lease_token = $3, lease_until = $4 "
                    "WHERE job_id = $1 AND scheduled_for = $2",
                    row["job_id"], row["scheduled_for"], token, until,
                )
        return [
            Execution.model_validate({**dict(row), "lease_token": token, "lease_until": until})
            for row in rows
        ]
    with _lock:
        found = []
        for path in (store.data_dir() / "jobs").glob("execution-*.json"):
            execution = _read_execution_path(path)
            if (
                execution is None
                or execution.run_id is not None
                or _read_job(execution.job_id) is None
                or execution.lease_until is not None and execution.lease_until > now
            ):
                continue
            execution.lease_token = token
            execution.lease_until = until
            path.write_text(execution.model_dump_json(), encoding="utf-8")
            found.append(execution)
        return found


async def mark_started(execution: Execution, run_id: str) -> bool:
    """Commit a started run only while this heartbeat owns the execution lease."""
    if store.use_postgres():
        from store import db

        result = await (await db.pool()).execute(
            "UPDATE hatchery_job_executions SET run_id = $4, lease_token = NULL, lease_until = NULL "
            "WHERE job_id = $1 AND scheduled_for = $2 AND lease_token = $3 AND run_id IS NULL",
            execution.job_id, execution.scheduled_for, execution.lease_token, run_id,
        )
        return result != "UPDATE 0"
    with _lock:
        path = _execution_path(execution.job_id, execution.scheduled_for)
        current = _read_execution_path(path)
        if (
            current is None
            or current.run_id is not None
            or current.lease_token != execution.lease_token
        ):
            return False
        current.run_id = run_id
        current.lease_token = None
        current.lease_until = None
        path.write_text(current.model_dump_json(), encoding="utf-8")
        return True


async def claim_run(turn_id: str, run_id: str) -> bool:
    """Atomically let one workflow run own a stable scheduled turn."""
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "UPDATE hatchery_job_executions e SET run_id = $2, lease_token = NULL, lease_until = NULL "
            "FROM hatchery_jobs j WHERE e.turn_id = $1 AND e.job_id = j.id "
            "AND NOT j.paused AND (e.run_id IS NULL OR e.run_id = $2) RETURNING e.run_id",
            turn_id, run_id,
        )
        return row is not None
    with _lock:
        for path in (store.data_dir() / "jobs").glob("execution-*.json"):
            execution = _read_execution_path(path)
            if execution is None:
                continue
            job = _read_job(execution.job_id)
            if execution.turn_id != turn_id or job is None or job.paused:
                continue
            if execution.run_id not in {None, run_id}:
                return False
            execution.run_id = run_id
            execution.lease_token = None
            execution.lease_until = None
            path.write_text(execution.model_dump_json(), encoding="utf-8")
            return True
        return False


async def started_run(turn_id: str) -> str | None:
    """Return the committed run for a stable scheduled turn identity."""
    if store.use_postgres():
        from store import db

        return await (await db.pool()).fetchval(
            "SELECT run_id FROM hatchery_job_executions WHERE turn_id = $1", turn_id
        )
    with _lock:
        for path in (store.data_dir() / "jobs").glob("execution-*.json"):
            execution = _read_execution_path(path)
            if execution is not None and execution.turn_id == turn_id:
                return execution.run_id
        return None


async def cleanup(now: datetime.datetime | None = None) -> int:
    """Remove terminal execution bookkeeping after the retention window."""
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - _RETENTION
    if store.use_postgres():
        from store import db

        result = await (await db.pool()).execute(
            "DELETE FROM hatchery_job_executions WHERE run_id IS NOT NULL AND created_at < $1",
            cutoff,
        )
        return int(result.split()[-1])
    removed = 0
    with _lock:
        for path in (store.data_dir() / "jobs").glob("execution-*.json"):
            execution = _read_execution_path(path)
            if (
                execution is not None
                and execution.run_id is not None
                and execution.scheduled_for < cutoff
            ):
                path.unlink()
                removed += 1
    return removed


def _chat_for(job: models.Job) -> models.Chat:
    title = job.prompt.strip().splitlines()[0][:80] or "scheduled job"
    return models.Chat(
        id=f"chat_{uuid.uuid4().hex[:12]}", user_id=job.owner_id, space_id=job.space_id,
        title=title, trigger=f"cron:{job.id}", created_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )


def _job(row) -> models.Job:
    return models.Job(
        id=row["id"], space_id=row["space_id"], owner_id=row["owner_id"],
        schedule=row["schedule"], prompt=row["prompt"], paused=row["paused"],
        next_run_at=row["next_run_at"].isoformat(), created_at=row["created_at"].isoformat(),
    )


def _path(job_id: str):
    return store.data_dir() / "jobs" / f"{urllib.parse.quote(job_id, safe='')}.json"


def _execution_path(job_id: str, scheduled_for: datetime.datetime):
    token = urllib.parse.quote(f"{job_id}-{scheduled_for.isoformat()}", safe="")
    return store.data_dir() / "jobs" / f"execution-{token}.json"


def _read_job(job_id: str) -> models.Job | None:
    return _read_job_path(_path(job_id))


def _read_job_path(path) -> models.Job | None:
    if not path.exists() or path.name.startswith("execution-"):
        return None
    try:
        return models.Job.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None


def _write_job(job: models.Job) -> None:
    path = _path(job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(job.model_dump_json(), encoding="utf-8")


async def _delete_pending_chats(conn, chat_ids: list[str]) -> None:
    if not chat_ids:
        return
    await conn.execute("DELETE FROM hatchery_events WHERE stream_id = ANY($1::text[])", chat_ids)
    await conn.execute("DELETE FROM hatchery_streams WHERE stream_id = ANY($1::text[])", chat_ids)
    await conn.execute("DELETE FROM hatchery_chats WHERE id = ANY($1::text[])", chat_ids)


def _delete_pending_executions(job_id: str) -> None:
    for path in (store.data_dir() / "jobs").glob("execution-*.json"):
        execution = _read_execution_path(path)
        if execution is not None and execution.job_id == job_id and execution.run_id is None:
            path.unlink()
            for event_path in (store.data_dir() / "events").glob(
                f"{urllib.parse.quote(execution.chat_id, safe='')}.*.jsonl"
            ):
                event_path.unlink()
            chat_path = store.data_dir() / "chats" / f"{urllib.parse.quote(execution.chat_id, safe='')}.json"
            if chat_path.exists():
                chat_path.unlink()


def _prompt_message(execution: Execution) -> ai.messages.Message:
    message = ai.user_message(execution.prompt)
    message.id = f"job_prompt_{execution.job_id}_{int(execution.scheduled_for.timestamp())}"
    return message


def _write_prompt(execution: Execution, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_prompt_message(execution).model_dump_json() + "\n", encoding="utf-8")


def _read_execution_path(path) -> Execution | None:
    if not path.exists():
        return None
    try:
        return Execution.model_validate_json(path.read_text(encoding="utf-8"))
    except pydantic.ValidationError:
        return None
