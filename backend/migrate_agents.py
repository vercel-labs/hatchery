"""One-shot import of legacy Space definitions into Agents and private Git storage."""

import argparse
import asyncio
import json

import models
import store
from store import agent_files, agents


async def legacy_spaces() -> list[dict]:
    if store.use_postgres():
        from store import db

        rows = await (await db.pool()).fetch(
            "SELECT data FROM hatchery_spaces ORDER BY created_at"
        )
        return [
            json.loads(row["data"])
            if isinstance(row["data"], str)
            else dict(row["data"])
            for row in rows
        ]
    found = []
    directory = store.data_dir() / "spaces"
    for path in sorted(directory.glob("*.json")):
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return found


async def import_space(raw: dict) -> models.Agent:
    name = str(raw.get("name") or "agent").strip() or "agent"
    slug = agents.slug_for(name)
    existing = await agents.get_by_slug(slug)
    if existing is None:
        created = await agents.create(name, slug)
    else:
        created = existing
    updated = models.Agent(
        **{
            **created.model_dump(),
            "name": name,
            "about": str(raw.get("about") or ""),
            "repos": raw.get("repos") or [],
            "resources": raw.get("resources") or [],
            "color": raw.get("color") or created.color,
        }
    )
    await agents.save(updated)

    legacy_id = str(raw.get("id") or slug)
    for _ in range(4):
        snapshot = await agent_files.snapshot(updated.slug)
        try:
            await agent_files.scaffold(
                updated.slug,
                operation_id=f"migrate-scaffold:{legacy_id}",
                expected_revision=snapshot.revision,
            )
            break
        except agent_files.Conflict:
            continue
    else:
        raise RuntimeError(f"storage kept changing while scaffolding {updated.slug}")

    snapshot = await agent_files.snapshot(updated.slug)
    instructions = updated.about.strip() or f"# {updated.name}\n"
    try:
        await agent_files.write(
            updated.slug,
            "AGENTS.md",
            instructions.rstrip() + "\n",
            operation_id=f"migrate-instructions:{legacy_id}",
            expected_revision=snapshot.revision,
        )
    except agent_files.Conflict as error:
        raise RuntimeError(
            f"storage changed while importing AGENTS.md for {updated.slug}"
        ) from error
    return updated


async def migrate() -> list[models.Agent]:
    if not await agent_files.configured():
        raise RuntimeError("HATCHERY_AGENTS_REPOSITORY_URL is required")
    await agents.ensure_ready()
    imported = []
    for raw in await legacy_spaces():
        imported.append(await import_space(raw))
    return imported


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import legacy spaces only; chats and runtime history are ignored."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirm that only Space definitions should be imported",
    )
    args = parser.parse_args()
    if not args.confirm:
        parser.error("pass --confirm after backing up the legacy deployment")
    imported = await migrate()
    print(json.dumps([agent.model_dump(mode="json") for agent in imported], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
