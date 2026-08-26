"""Users, Hatchery sessions, OAuth state, and Vercel connections."""

import asyncio
import datetime
import hashlib
import json
import secrets
import threading
import urllib.parse

import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_users (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hatchery_sessions (
    id_hash    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hatchery_sessions_user ON hatchery_sessions (user_id);
CREATE TABLE IF NOT EXISTS hatchery_vercel_connections (
    user_id    TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hatchery_oauth_states (
    state_hash TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_lock = threading.Lock()
_refresh_locks: dict[str, asyncio.Lock] = {}
_schema_ready = False


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        for name in ("users", "sessions", "connections", "oauth"):
            (store.data_dir() / "auth" / name).mkdir(parents=True, exist_ok=True)


async def save_user(user: dict) -> dict:
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_users (id, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            user["id"],
            json.dumps(user),
        )
    else:
        with _lock:
            _write("users", user["id"], user)
    return user


async def get_user(user_id: str) -> dict | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_users WHERE id = $1", user_id
        )
        return _data(row["data"]) if row else None
    with _lock:
        return _read("users", user_id)


async def create_session(user_id: str, lifetime: datetime.timedelta) -> str:
    session_id = secrets.token_urlsafe(32)
    expires_at = now() + lifetime
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_sessions (id_hash, user_id, expires_at) VALUES ($1, $2, $3)",
            hash_secret(session_id),
            user_id,
            expires_at,
        )
    else:
        with _lock:
            _write(
                "sessions",
                hash_secret(session_id),
                {"user_id": user_id, "expires_at": expires_at.isoformat()},
            )
    return session_id


async def session_user(session_id: str) -> dict | None:
    key = hash_secret(session_id)
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT user_id FROM hatchery_sessions WHERE id_hash = $1 AND expires_at > now()",
            key,
        )
        return await get_user(row["user_id"]) if row else None
    with _lock:
        session = _read("sessions", key)
    if session is None or session["expires_at"] <= now().isoformat():
        return None
    return await get_user(session["user_id"])


async def delete_session(session_id: str) -> None:
    key = hash_secret(session_id)
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "DELETE FROM hatchery_sessions WHERE id_hash = $1", key
        )
    else:
        with _lock:
            path = _path("sessions", key)
            if path.exists():
                path.unlink()


async def save_connection(connection: dict) -> dict:
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_vercel_connections (user_id, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            connection["user_id"],
            json.dumps(connection),
        )
    else:
        with _lock:
            _write("connections", connection["user_id"], connection)
    return connection


async def get_connection(user_id: str) -> dict | None:
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_vercel_connections WHERE user_id = $1", user_id
        )
        return _data(row["data"]) if row else None
    with _lock:
        return _read("connections", user_id)


def refresh_lock(user_id: str) -> asyncio.Lock:
    # One service process serializes local requests. Postgres updates remain
    # atomic; a refresh conflict from another process is surfaced as re-login.
    return _refresh_locks.setdefault(user_id, asyncio.Lock())


async def save_oauth_state(state: str, data: dict, lifetime: datetime.timedelta) -> None:
    expires_at = now() + lifetime
    key = hash_secret(state)
    data = {**data, "expires_at": expires_at.isoformat()}
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_oauth_states (state_hash, data, expires_at) VALUES ($1, $2::jsonb, $3)",
            key,
            json.dumps(data),
            expires_at,
        )
    else:
        with _lock:
            _write("oauth", key, data)


async def consume_oauth_state(state: str) -> dict | None:
    key = hash_secret(state)
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "DELETE FROM hatchery_oauth_states WHERE state_hash = $1 AND expires_at > now() RETURNING data",
            key,
        )
        return _data(row["data"]) if row else None
    with _lock:
        data = _read("oauth", key)
        path = _path("oauth", key)
        if path.exists():
            path.unlink()
    if data is None or data["expires_at"] <= now().isoformat():
        return None
    return data


def _data(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _path(kind: str, key: str):
    return store.data_dir() / "auth" / kind / f"{urllib.parse.quote(key, safe='')}.json"


def _write(kind: str, key: str, data: dict) -> None:
    path = _path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def _read(kind: str, key: str) -> dict | None:
    path = _path(kind, key)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
