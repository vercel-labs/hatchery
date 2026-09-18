"""App-wide settings shared by browser, channel, and scheduled runs."""

import json

import models
import store

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS hatchery_settings (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_schema_ready = False


async def ensure_ready() -> None:
    global _schema_ready
    if store.use_postgres():
        if not _schema_ready:
            from store import db

            await (await db.pool()).execute(_SCHEMA)
            _schema_ready = True
    else:
        store.data_dir().mkdir(parents=True, exist_ok=True)


async def get() -> models.AppSettings:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        row = await (await db.pool()).fetchrow(
            "SELECT data FROM hatchery_settings WHERE id = 'app'"
        )
        if row is None:
            return models.AppSettings()
        raw = row["data"]
        return models.AppSettings.model_validate_json(
            raw if isinstance(raw, str) else json.dumps(raw)
        )

    path = store.data_dir() / "settings.json"
    if not path.exists():
        return models.AppSettings()
    return models.AppSettings.model_validate_json(path.read_text(encoding="utf-8"))


async def save(value: models.AppSettings) -> models.AppSettings:
    await ensure_ready()
    if store.use_postgres():
        from store import db

        await (await db.pool()).execute(
            "INSERT INTO hatchery_settings (id, data) VALUES ('app', $1::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            value.model_dump_json(),
        )
        return value

    path = store.data_dir() / "settings.json"
    path.write_text(value.model_dump_json(), encoding="utf-8")
    return value
