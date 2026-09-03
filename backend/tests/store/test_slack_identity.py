import contextlib

import asyncpg
import pytest

from store import auth


class Connection:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return contextlib.nullcontext()

    async def execute(self, query, *args):
        if query.startswith("DELETE FROM hatchery_slack_identities"):
            self.pool.identities = {
                identity: owner
                for identity, owner in self.pool.identities.items()
                if owner != args[0]
            }
        elif query.startswith("INSERT INTO hatchery_slack_identities"):
            identity = (args[0], args[1])
            owner = self.pool.identities.get(identity)
            if owner is not None and owner != args[2]:
                error = asyncpg.UniqueViolationError("duplicate")
                error.constraint_name = "hatchery_slack_identity"
                raise error
            if args[2] in self.pool.identities.values():
                error = asyncpg.UniqueViolationError("one workspace per user")
                error.constraint_name = "hatchery_slack_user"
                raise error
            self.pool.identities[identity] = args[2]
        elif "jsonb_set" in query:
            self.pool.connections[args[0]] = args[1]
            return "UPDATE 1"
        elif "data = data - 'slack'" in query:
            self.pool.connections.pop(args[0], None)
        return "OK"


class Acquire:
    def __init__(self, pool):
        self.connection = Connection(pool)

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class Pool:
    def __init__(self):
        self.identities = {}
        self.connections = {}

    def acquire(self):
        return Acquire(self)

    async def fetchrow(self, _query, team_id, slack_user_id):
        user_id = self.identities.get((team_id, slack_user_id))
        return {"user_id": user_id} if user_id else None


async def test_slack_identity_is_workspace_scoped_unique_and_removed(monkeypatch):
    pool = Pool()

    async def get_pool():
        return pool

    monkeypatch.setattr(auth.db, "pool", get_pool)
    connection = {"team_id": "T1", "user_id": "U1", "team": "Acme"}

    await auth.save_slack_connection("hatchery_1", connection)

    assert await auth.slack_user("T1", "U1") == "hatchery_1"
    assert await auth.slack_user("T2", "U1") is None
    assert await auth.slack_user("", "U1") is None
    assert "xox" not in pool.connections["hatchery_1"]

    with pytest.raises(auth.SlackIdentityConflict):
        await auth.save_slack_connection("hatchery_2", connection)

    replacement = {"team_id": "T2", "user_id": "U2", "team": "Other"}
    await auth.save_slack_connection("hatchery_1", replacement)
    assert await auth.slack_user("T1", "U1") is None
    assert await auth.slack_user("T2", "U2") == "hatchery_1"

    await auth.delete_slack_connection("hatchery_1")
    assert await auth.slack_user("T2", "U2") is None
    assert "hatchery_1" not in pool.connections
