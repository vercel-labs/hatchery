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
CREATE TABLE IF NOT EXISTS hatchery_user_secrets (
    user_id    TEXT NOT NULL REFERENCES hatchery_users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, name)
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
        "ON CONFLICT (id) DO UPDATE SET data = hatchery_users.data || EXCLUDED.data",
        user["id"],
        json.dumps(user),
    )
    return user


async def get_user(user_id: str) -> dict | None:
    row = await (await db.pool()).fetchrow(
        "SELECT data FROM hatchery_users WHERE id = $1", user_id
    )
    if row is None:
        return None
    raw = row["data"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def save_connection(user_id: str, name: str, connection: dict) -> None:
    await (await db.pool()).execute(
        "UPDATE hatchery_users SET data = jsonb_set(data, ARRAY[$2], $3::jsonb) WHERE id = $1",
        user_id,
        name,
        json.dumps(connection),
    )


async def delete_connection(user_id: str, name: str) -> None:
    await (await db.pool()).execute(
        "UPDATE hatchery_users SET data = data - $2 WHERE id = $1",
        user_id,
        name,
    )


async def save_github_connection(user_id: str, connection: dict) -> None:
    await save_connection(user_id, "github", connection)


async def save_github_installation(
    user_id: str, owner: str, installation_id: str
) -> None:
    await (await db.pool()).execute(
        "UPDATE hatchery_users SET data = jsonb_set("
        "data, '{github}', COALESCE(data->'github', '{}'::jsonb) || "
        "jsonb_build_object('installations', "
        "COALESCE(data->'github'->'installations', '{}'::jsonb) || "
        "jsonb_build_object($2::text, $3::text)), true) WHERE id = $1",
        user_id,
        owner.lower(),
        installation_id,
    )


async def delete_github_connection(user_id: str) -> None:
    await delete_connection(user_id, "github")


async def save_secret(user_id: str, name: str, value: str) -> None:
    await (await db.pool()).execute(
        "INSERT INTO hatchery_user_secrets (user_id, name, value) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id, name) DO UPDATE SET value = EXCLUDED.value, created_at = now()",
        user_id,
        name,
        value,
    )


async def get_secret(user_id: str, name: str) -> str | None:
    row = await (await db.pool()).fetchrow(
        "SELECT value FROM hatchery_user_secrets WHERE user_id = $1 AND name = $2",
        user_id,
        name,
    )
    return str(row["value"]) if row is not None else None


async def delete_secret(user_id: str, name: str) -> None:
    await (await db.pool()).execute(
        "DELETE FROM hatchery_user_secrets WHERE user_id = $1 AND name = $2",
        user_id,
        name,
    )


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
