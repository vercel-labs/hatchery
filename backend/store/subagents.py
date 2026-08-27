"""Durable subagents, each bound to one chat-owned devbox."""

import datetime
import json
import threading
import urllib.parse
import uuid

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_subagents (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_subagents_chat ON hatchery_subagents (chat_id, created_at);
"""

_lock = threading.Lock()
_schema_ready = False


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        (store.data_dir() / "subagents").mkdir(parents=True, exist_ok=True)


async def create(
    chat_id: str, devbox_id: str, prompt: str, webhook_secret: str
) -> dict:
    record = {
        "id": f"subagent_{uuid.uuid4().hex[:12]}",
        "chat_id": chat_id,
        "devbox_id": devbox_id,
        "title": prompt.strip().splitlines()[0][:80] or "subagent",
        "prompt": prompt,
        "state": "creating",
        "webhook_secret": webhook_secret,
        "webhook_seq": 0,
        "completion_delivered": False,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    await save(record)
    return record


async def finish_create(task_id: str, created: dict) -> dict:
    """Add control-plane ids without regressing an early webhook state."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM hatchery_subagents WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                raise KeyError(task_id)
            record = _data(row["data"])
            record["task_id"] = created["task_id"]
            record["session_id"] = created["session_id"]
            if record.get("state") == "creating":
                record["state"] = created["state"]
            await conn.execute(
                "UPDATE hatchery_subagents SET data = $2::jsonb WHERE id = $1",
                task_id,
                json.dumps(record, separators=(",", ":")),
            )
            return record
    path = _path(task_id)
    with _lock:
        if not path.exists():
            raise KeyError(task_id)
        record = json.loads(path.read_text())
        record["task_id"] = created["task_id"]
        record["session_id"] = created["session_id"]
        if record.get("state") == "creating":
            record["state"] = created["state"]
        path.write_text(json.dumps(record, separators=(",", ":")))
        return record


async def save(record: dict) -> None:
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_subagents (id, chat_id, data) VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            record["id"],
            record["chat_id"],
            json.dumps(record, separators=(",", ":")),
        )
        return
    path = _path(record["id"])
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, separators=(",", ":")))


async def get(task_id: str) -> dict | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_subagents WHERE id = $1", task_id
        )
        return _data(row["data"]) if row is not None else None
    path = _path(task_id)
    with _lock:
        return json.loads(path.read_text()) if path.exists() else None


async def delete(task_id: str) -> bool:
    if store.use_postgres():
        from store import db

        result = await (await db.pool()).execute(
            "DELETE FROM hatchery_subagents WHERE id = $1", task_id
        )
        return result != "DELETE 0"
    path = _path(task_id)
    with _lock:
        if not path.exists():
            return False
        path.unlink()
        return True


async def delete_for_devbox(devbox_id: str) -> None:
    for record in await list_for_devbox(devbox_id):
        await delete(record["id"])


async def list_for_devbox(devbox_id: str) -> list[dict]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_subagents WHERE data->>'devbox_id' = $1 ORDER BY created_at",
            devbox_id,
        )
        return [_data(row["data"]) for row in rows]
    with _lock:
        found = []
        for path in (store.data_dir() / "subagents").glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("devbox_id") == devbox_id:
                found.append(record)
        return sorted(found, key=lambda record: record.get("created_at", ""))


async def apply_state(
    task_id: str,
    state: str,
    result: dict | None = None,
    *,
    seq: int | None = None,
    reconcile: bool = False,
    remote_task_id: str | None = None,
) -> tuple[dict, bool, str]:
    """Atomically apply a pushed or reconciled state without regressing completion."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM hatchery_subagents WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                raise KeyError(task_id)
            record = _data(row["data"])
            previous = str(record.get("state", ""))
            current_seq = int(record.get("webhook_seq") or 0)
            stale = seq is not None and seq <= current_seq
            regresses = previous in ("complete", "errored") and state not in (
                "complete",
                "errored",
            )
            old_resume = (
                reconcile
                and record.get("awaiting_resume")
                and state
                in (
                    "complete",
                    "errored",
                )
            )
            if stale or regresses or old_resume:
                return record, False, previous
            record["state"] = state
            if remote_task_id is not None:
                record["task_id"] = remote_task_id
            if seq is not None:
                record["webhook_seq"] = seq
            if result is not None:
                record["result"] = result
            if reconcile and state not in ("complete", "errored"):
                record.pop("awaiting_resume", None)
            await conn.execute(
                "UPDATE hatchery_subagents SET data = $2::jsonb WHERE id = $1",
                task_id,
                json.dumps(record, separators=(",", ":")),
            )
            return record, state != previous, previous
    path = _path(task_id)
    with _lock:
        if not path.exists():
            raise KeyError(task_id)
        record = json.loads(path.read_text())
        previous = str(record.get("state", ""))
        current_seq = int(record.get("webhook_seq") or 0)
        stale = seq is not None and seq <= current_seq
        regresses = previous in ("complete", "errored") and state not in (
            "complete",
            "errored",
        )
        old_resume = (
            reconcile
            and record.get("awaiting_resume")
            and state
            in (
                "complete",
                "errored",
            )
        )
        if stale or regresses or old_resume:
            return record, False, previous
        record["state"] = state
        if remote_task_id is not None:
            record["task_id"] = remote_task_id
        if seq is not None:
            record["webhook_seq"] = seq
        if result is not None:
            record["result"] = result
        if reconcile and state not in ("complete", "errored"):
            record.pop("awaiting_resume", None)
        path.write_text(json.dumps(record, separators=(",", ":")))
        return record, state != previous, previous


async def resume(task_id: str) -> dict:
    """Reset completion state after more input was delivered to the same task."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM hatchery_subagents WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                raise KeyError(task_id)
            record = _data(row["data"])
            record["state"] = "running"
            record["completion_delivered"] = False
            record["awaiting_resume"] = True
            for key in ("result", "completion_message"):
                record.pop(key, None)
            await conn.execute(
                "UPDATE hatchery_subagents SET data = $2::jsonb WHERE id = $1",
                task_id,
                json.dumps(record, separators=(",", ":")),
            )
            return record
    path = _path(task_id)
    with _lock:
        if not path.exists():
            raise KeyError(task_id)
        record = json.loads(path.read_text())
        record["state"] = "running"
        record["completion_delivered"] = False
        record["awaiting_resume"] = True
        for key in ("result", "completion_message"):
            record.pop(key, None)
        path.write_text(json.dumps(record, separators=(",", ":")))
        return record


async def claim_completion(task_id: str) -> dict | None:
    """Claim terminal report delivery with an expiring crash-safe lease."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM hatchery_subagents WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                return None
            record = _data(row["data"])
            lease = record.get("completion_lease_until")
            busy = bool(
                lease and lease > datetime.datetime.now(datetime.UTC).isoformat()
            )
            if (
                busy
                or record.get("completion_delivered")
                or record.get("state")
                not in (
                    "complete",
                    "errored",
                )
            ):
                return None
            record["completion_lease_until"] = (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)
            ).isoformat()
            record["completion_generation"] = (
                int(record.get("completion_generation") or 0) + 1
            )
            await conn.execute(
                "UPDATE hatchery_subagents SET data = $2::jsonb WHERE id = $1",
                task_id,
                json.dumps(record, separators=(",", ":")),
            )
            return record
    path = _path(task_id)
    with _lock:
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        lease = record.get("completion_lease_until")
        busy = bool(lease and lease > datetime.datetime.now(datetime.UTC).isoformat())
        if (
            busy
            or record.get("completion_delivered")
            or record.get("state")
            not in (
                "complete",
                "errored",
            )
        ):
            return None
        record["completion_lease_until"] = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)
        ).isoformat()
        record["completion_generation"] = (
            int(record.get("completion_generation") or 0) + 1
        )
        path.write_text(json.dumps(record, separators=(",", ":")))
        return record


async def finish_completion(task_id: str, generation: int, **updates) -> dict | None:
    """Release a completion claim without overwriting a newer generation."""
    if store.use_postgres():
        from store import db

        pool = await db.pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT data FROM hatchery_subagents WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                return None
            record = _data(row["data"])
            if int(record.get("completion_generation") or 0) != generation:
                return record
            record.update(updates)
            record.pop("completion_lease_until", None)
            await conn.execute(
                "UPDATE hatchery_subagents SET data = $2::jsonb WHERE id = $1",
                task_id,
                json.dumps(record, separators=(",", ":")),
            )
            return record
    path = _path(task_id)
    with _lock:
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        if int(record.get("completion_generation") or 0) != generation:
            return record
        record.update(updates)
        record.pop("completion_lease_until", None)
        path.write_text(json.dumps(record, separators=(",", ":")))
        return record


async def list_for_chat(chat_id: str) -> list[dict]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_subagents WHERE chat_id = $1 ORDER BY created_at",
            chat_id,
        )
        return [_data(row["data"]) for row in rows]
    with _lock:
        found = []
        for path in (store.data_dir() / "subagents").glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("chat_id") == chat_id:
                found.append(record)
        return sorted(found, key=lambda record: record.get("created_at", ""))


def _data(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else raw


def _path(task_id: str):
    return (
        store.data_dir() / "subagents" / f"{urllib.parse.quote(task_id, safe='')}.json"
    )
