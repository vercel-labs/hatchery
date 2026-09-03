"""Vercel users, OAuth state, and browser sessions in Postgres."""

import datetime
import hashlib
import json
import secrets

import asyncpg

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
CREATE TABLE IF NOT EXISTS hatchery_slack_identities (
    team_id    TEXT NOT NULL,
    slack_user_id TEXT NOT NULL,
    user_id    TEXT NOT NULL REFERENCES hatchery_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT hatchery_slack_identity PRIMARY KEY (team_id, slack_user_id),
    CONSTRAINT hatchery_slack_user UNIQUE (user_id)
);
CREATE TABLE IF NOT EXISTS hatchery_github_identities (
    github_user_id TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES hatchery_users(id) ON DELETE CASCADE UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO hatchery_github_identities (github_user_id, user_id)
SELECT data->'github'->>'id', id FROM hatchery_users
WHERE data->'github'->>'id' IS NOT NULL
ON CONFLICT DO NOTHING;
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


class GitHubIdentityConflict(RuntimeError):
    """A GitHub account is already linked to another Hatchery user."""


async def save_github_connection(user_id: str, connection: dict) -> None:
    pool = await db.pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM hatchery_github_identities WHERE user_id = $1", user_id
            )
            await conn.execute(
                "INSERT INTO hatchery_github_identities (github_user_id, user_id) VALUES ($1, $2)",
                connection["id"],
                user_id,
            )
            result = await conn.execute(
                "UPDATE hatchery_users SET data = jsonb_set(data, '{github}', $2::jsonb) WHERE id = $1",
                user_id,
                json.dumps(connection),
            )
            if result != "UPDATE 1":
                raise RuntimeError("unknown Hatchery user")
    except asyncpg.UniqueViolationError as error:
        raise GitHubIdentityConflict("GitHub account is already connected") from error


async def github_user(github_user_id: str) -> str | None:
    if not github_user_id:
        return None
    row = await (await db.pool()).fetchrow(
        "SELECT user_id FROM hatchery_github_identities WHERE github_user_id = $1",
        github_user_id,
    )
    return str(row["user_id"]) if row is not None else None


async def delete_github_connection(user_id: str) -> None:
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM hatchery_github_identities WHERE user_id = $1", user_id
        )
        await conn.execute(
            "UPDATE hatchery_users SET data = data - 'github' WHERE id = $1", user_id
        )


class SlackIdentityConflict(RuntimeError):
    """A Slack account is already linked to another Hatchery user."""


async def save_slack_connection(user_id: str, connection: dict) -> None:
    """Atomically replace one user's Slack metadata and reverse identity mapping."""
    pool = await db.pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM hatchery_slack_identities WHERE user_id = $1", user_id
            )
            await conn.execute(
                "INSERT INTO hatchery_slack_identities (team_id, slack_user_id, user_id) "
                "VALUES ($1, $2, $3)",
                connection["team_id"],
                connection["user_id"],
                user_id,
            )
            result = await conn.execute(
                "UPDATE hatchery_users SET data = jsonb_set(data, '{slack}', $2::jsonb) "
                "WHERE id = $1",
                user_id,
                json.dumps(connection),
            )
            if result != "UPDATE 1":
                raise RuntimeError("unknown Hatchery user")
    except asyncpg.UniqueViolationError as error:
        if error.constraint_name not in {
            "hatchery_slack_identity",
            "hatchery_slack_user",
        }:
            raise
        raise SlackIdentityConflict("Slack account is already connected") from error


async def slack_user(team_id: str, slack_user_id: str) -> str | None:
    if not team_id or not slack_user_id:
        return None
    row = await (await db.pool()).fetchrow(
        "SELECT user_id FROM hatchery_slack_identities "
        "WHERE team_id = $1 AND slack_user_id = $2",
        team_id,
        slack_user_id,
    )
    return str(row["user_id"]) if row is not None else None


async def delete_slack_connection(user_id: str) -> None:
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM hatchery_slack_identities WHERE user_id = $1", user_id
        )
        await conn.execute(
            "UPDATE hatchery_users SET data = data - 'slack' WHERE id = $1", user_id
        )


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
