"""Assign legacy chats to the sole owner and rewrite sidebar title fragments.

Preview first:
    uv run python scripts/backfill_existing_chats.py --output /tmp/chat-backfill.json

Review every before/after entry, then apply that exact plan:
    uv run python scripts/backfill_existing_chats.py --apply /tmp/chat-backfill.json

The apply is atomic, rejects database drift, and is safe to rerun.
"""

import argparse
import asyncio
import dataclasses
import json
import pathlib
import re
import sys

import asyncpg

from agent import topic
from store import db


@dataclasses.dataclass(frozen=True)
class Owner:
    id: str
    display_name: str


_ALLOWED_FRAGMENT = re.compile(r"(?:'[a-z0-9][a-z0-9 ]*|[a-z0-9][a-z0-9 ']*[a-z0-9])")


def display_name(user: dict) -> str | None:
    for field in ("name", "username", "email"):
        value = user.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def select_owner(users: list[dict], chat_user_ids: set[str]) -> Owner:
    users_by_id = {str(user["id"]): user for user in users}
    unknown = chat_user_ids - users_by_id.keys()
    if unknown:
        raise ValueError(f"chat owners missing from hatchery_users: {sorted(unknown)}")
    if len(chat_user_ids) > 1:
        raise ValueError(f"chats have multiple owners: {sorted(chat_user_ids)}")
    if chat_user_ids:
        owner_id = next(iter(chat_user_ids))
    elif len(users) == 1:
        owner_id = str(users[0]["id"])
    else:
        raise ValueError(
            f"owner is ambiguous: {len(users)} users and no common chat owner"
        )
    owner_name = display_name(users_by_id[owner_id])
    if owner_name is None:
        raise ValueError(f"owner {owner_id!r} has no name, username, or email")
    return Owner(owner_id, owner_name)


def validate_fragment(fragment: str) -> None:
    if not fragment:
        raise ValueError("title fragment is empty")
    if fragment != " ".join(fragment.split()):
        raise ValueError(f"title fragment has invalid spacing: {fragment!r}")
    if len(fragment) > 20:
        raise ValueError(f"title fragment exceeds 20 characters: {fragment!r}")
    if fragment != fragment.lower():
        raise ValueError(f"title fragment is not lowercase: {fragment!r}")
    if _ALLOWED_FRAGMENT.fullmatch(fragment) is None:
        raise ValueError(f"title fragment has invalid punctuation or spacing: {fragment!r}")


def _json(value) -> dict:
    return json.loads(value) if isinstance(value, str) else dict(value)


async def preview() -> dict:
    connection = await asyncpg.connect(db.direct_dsn(), statement_cache_size=0)
    try:
        user_rows = await connection.fetch(
            "SELECT id, data FROM hatchery_users ORDER BY created_at, id"
        )
        chat_rows = await connection.fetch(
            "SELECT id, data FROM hatchery_chats ORDER BY created_at, id"
        )
    finally:
        await connection.close()

    users = []
    for row in user_rows:
        user = _json(row["data"])
        user["id"] = row["id"]
        users.append(user)
    chats = [(str(row["id"]), _json(row["data"])) for row in chat_rows]
    owner = select_owner(
        users,
        {str(chat["user_id"]) for _, chat in chats if chat.get("user_id")},
    )

    changes = []
    for chat_id, chat in chats:
        source = str(chat.get("topic") or chat.get("title") or "").strip()
        if not source:
            raise ValueError(f"chat {chat_id} has no title or topic")
        fragment = await topic.generate(source)
        validate_fragment(fragment)
        before = {
            "user_id": chat.get("user_id"),
            "author_display_name": chat.get("author_display_name"),
            "topic": chat.get("topic"),
        }
        after = {
            "user_id": owner.id,
            "author_display_name": owner.display_name,
            "topic": fragment,
        }
        changes.append(
            {
                "id": chat_id,
                "source_title": source,
                "before": before,
                "after": after,
            }
        )

    return {
        "version": 1,
        "owner": dataclasses.asdict(owner),
        "chat_count": len(changes),
        "changes": changes,
    }


def validate_plan(plan: dict) -> None:
    if plan.get("version") != 1:
        raise ValueError("unsupported plan version")
    changes = plan.get("changes")
    if not isinstance(changes, list) or plan.get("chat_count") != len(changes):
        raise ValueError("plan chat_count does not match changes")
    ids = [change.get("id") for change in changes]
    if any(not isinstance(chat_id, str) or not chat_id for chat_id in ids):
        raise ValueError("every planned chat needs an id")
    if len(ids) != len(set(ids)):
        raise ValueError("plan contains duplicate chat ids")
    owner = plan.get("owner") or {}
    for change in changes:
        after = change.get("after") or {}
        if after.get("user_id") != owner.get("id"):
            raise ValueError(f"chat {change['id']} does not use the planned owner")
        if after.get("author_display_name") != owner.get("display_name"):
            raise ValueError(f"chat {change['id']} does not use the planned author")
        validate_fragment(after.get("topic", ""))


async def apply(plan: dict) -> tuple[int, int]:
    validate_plan(plan)
    connection = await asyncpg.connect(db.direct_dsn(), statement_cache_size=0)
    updated = 0
    unchanged = 0
    try:
        async with connection.transaction():
            user_rows = await connection.fetch(
                "SELECT id, data FROM hatchery_users ORDER BY created_at, id FOR UPDATE"
            )
            chat_rows = await connection.fetch(
                "SELECT id, data FROM hatchery_chats ORDER BY created_at, id FOR UPDATE"
            )
            users = []
            for row in user_rows:
                user = _json(row["data"])
                user["id"] = row["id"]
                users.append(user)
            current = {str(row["id"]): _json(row["data"]) for row in chat_rows}
            planned_ids = {change["id"] for change in plan["changes"]}
            if current.keys() != planned_ids:
                raise ValueError(
                    "database chat set changed after preview; generate a new plan"
                )
            owner = select_owner(
                users,
                {
                    str(chat["user_id"])
                    for chat in current.values()
                    if chat.get("user_id")
                },
            )
            if dataclasses.asdict(owner) != plan["owner"]:
                raise ValueError("database owner changed after preview; generate a new plan")

            for change in plan["changes"]:
                chat = current[change["id"]]
                fields = {
                    "user_id": chat.get("user_id"),
                    "author_display_name": chat.get("author_display_name"),
                    "topic": chat.get("topic"),
                }
                if fields == change["after"]:
                    unchanged += 1
                    continue
                if fields != change["before"]:
                    raise ValueError(
                        f"chat {change['id']} changed after preview; generate a new plan"
                    )
                await connection.execute(
                    "UPDATE hatchery_chats SET data = data || $2::jsonb WHERE id = $1",
                    change["id"],
                    json.dumps(change["after"], separators=(",", ":")),
                )
                updated += 1
    finally:
        await connection.close()
    return updated, unchanged


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, help="write preview plan here")
    parser.add_argument("--apply", type=pathlib.Path, help="apply an reviewed preview plan")
    args = parser.parse_args()
    if args.apply:
        plan = json.loads(args.apply.read_text(encoding="utf-8"))
        updated, unchanged = await apply(plan)
        print(json.dumps({"updated": updated, "unchanged": unchanged}, indent=2))
        return
    plan = await preview()
    rendered = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {plan['chat_count']} chats to {args.output}", file=sys.stderr)
    print(rendered, end="")


if __name__ == "__main__":
    asyncio.run(main())
