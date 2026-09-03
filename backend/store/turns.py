"""Durable ownership and workflow discovery for dispatcher turns."""

import asyncio
import contextlib
import contextvars
import dataclasses
import typing

import store
from store import events


@dataclasses.dataclass(frozen=True)
class ActiveTurn:
    turn_id: str
    run_id: str
    origin: str
    task_id: str | None
    generation: int


class BusyError(RuntimeError):
    pass


_locks: dict[str, asyncio.Lock] = {}
_held: typing.ContextVar[frozenset[str]]

# Context-local reentrancy lets worker completion persist its result and call the
# shared startup operation under one chat lock.
_held = contextvars.ContextVar("held_turn_locks", default=frozenset())


@contextlib.asynccontextmanager
async def run(chat_id: str):
    """Serialize short startup/commit transactions for one chat."""
    if chat_id in _held.get():
        yield
        return
    token = _held.set(_held.get() | {chat_id})
    try:
        async with _run_once(chat_id):
            yield
    finally:
        _held.reset(token)


@contextlib.asynccontextmanager
async def _run_once(chat_id: str):
    if store.use_postgres():
        from store import db

        key = f"turn:{chat_id}"
        async with (await db.pool()).acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtext($1))", key)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", key)
        return

    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        yield


async def active(chat_id: str) -> ActiveTurn | None:
    """Fold the registry and return its newest unterminated turn."""
    started: list[ActiveTurn] = []
    terminal: set[str] = set()
    for index, data in await events.read(chat_id, "turns"):
        event_type = data.get("type")
        turn_id = data.get("turn_id")
        if not isinstance(turn_id, str):
            continue
        if event_type == "turn.started":
            started.append(
                ActiveTurn(
                    turn_id=turn_id,
                    run_id=str(data["run_id"]),
                    origin=str(data["origin"]),
                    task_id=typing.cast(str | None, data.get("task_id")),
                    generation=index,
                )
            )
        elif event_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
            terminal.add(turn_id)
    return next((turn for turn in reversed(started) if turn.turn_id not in terminal), None)


async def finish(
    chat_id: str,
    turn_id: str,
    run_id: str,
    state: typing.Literal["completed", "failed", "cancelled"],
    error: str | None = None,
) -> int:
    """Idempotently record one terminal lifecycle event."""
    async with run(chat_id):
        records = await events.read(chat_id, "turns")
        for index, data in records:
            if data.get("turn_id") == turn_id and data.get("type") in {
                "turn.completed",
                "turn.failed",
                "turn.cancelled",
            }:
                return index
        data: dict[str, typing.Any] = {
            "type": f"turn.{state}",
            "turn_id": turn_id,
            "run_id": run_id,
        }
        if error is not None:
            data["error"] = error
        return await events.append(chat_id, "turns", data)
