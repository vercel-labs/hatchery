"""Vercel users, OAuth state, and browser sessions in Postgres."""

import datetime
import hashlib
import json
import secrets

from store import db

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_users (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hatchery_sessions (
    id_hash    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES hatchery_users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_sessions_user ON hatchery_sessions (user_id);
CREATE TABLE IF NOT EXISTS hatchery_oauth_states (
    state_hash TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def ensure_ready() -> None:
    await (await db.pool()).execute(_SCHEMA)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def save_user(user: dict) -> dict:
    await (await db.pool()).execute(
        "INSERT INTO hatchery_users (id, data) VALUES ($1, $2::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
        user["id"],
        json.dumps(user),
    )
    return user


async def create_session(user_id: str, lifetime: datetime.timedelta) -> str:
    session_id = secrets.token_urlsafe(32)
    await (await db.pool()).execute(
        "INSERT INTO hatchery_sessions (id_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        hash_secret(session_id),
        user_id,
        now() + lifetime,
    )
    return session_id


async def session_user(session_id: str) -> dict | None:
    row = await (await db.pool()).fetchrow(
        "SELECT users.data FROM hatchery_sessions sessions "
        "JOIN hatchery_users users ON users.id = sessions.user_id "
        "WHERE sessions.id_hash = $1 AND sessions.expires_at > now()",
        hash_secret(session_id),
    )
    if row is None:
        return None
    raw = row["data"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def delete_session(session_id: str) -> None:
    await (await db.pool()).execute(
        "DELETE FROM hatchery_sessions WHERE id_hash = $1", hash_secret(session_id)
    )


async def save_oauth_state(state: str, data: dict, lifetime: datetime.timedelta) -> None:
    await (await db.pool()).execute(
        "INSERT INTO hatchery_oauth_states (state_hash, data, expires_at) VALUES ($1, $2::jsonb, $3)",
        hash_secret(state),
        json.dumps(data),
        now() + lifetime,
    )


async def consume_oauth_state(state: str) -> dict | None:
    row = await (await db.pool()).fetchrow(
        "DELETE FROM hatchery_oauth_states "
        "WHERE state_hash = $1 AND expires_at > now() RETURNING data",
        hash_secret(state),
    )
    if row is None:
        return None
    raw = row["data"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)
