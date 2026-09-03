import copy

import asyncpg
import pytest

from store import auth


class Connection:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return Transaction(self.pool)

    async def execute(self, query, *args):
        if query.startswith("DELETE FROM hatchery_github_identities"):
            self.pool.identities = {
                identity: owner
                for identity, owner in self.pool.identities.items()
                if owner != args[0]
            }
        elif query.startswith("INSERT INTO hatchery_github_identities"):
            owner = self.pool.identities.get(args[0])
            if owner is not None and owner != args[1]:
                raise asyncpg.UniqueViolationError("duplicate")
            self.pool.identities[args[0]] = args[1]
        elif "jsonb_set" in query:
            self.pool.connections[args[0]] = args[1]
            return "UPDATE 1"
        elif "data = data - 'github'" in query:
            self.pool.connections.pop(args[0], None)
        return "OK"


class Transaction:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self.identities = copy.deepcopy(self.pool.identities)
        self.connections = copy.deepcopy(self.pool.connections)

    async def __aexit__(self, error_type, *_args):
        if error_type is not None:
            self.pool.identities = self.identities
            self.pool.connections = self.connections


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

    async def fetchrow(self, _query, github_user_id):
        user_id = self.identities.get(github_user_id)
        return {"user_id": user_id} if user_id else None


async def test_github_identity_is_unique_and_removed(monkeypatch):
    pool = Pool()

    async def get_pool():
        return pool

    monkeypatch.setattr(auth.db, "pool", get_pool)
    connection = {"id": "42", "login": "octocat"}

    await auth.save_github_connection("hatchery_1", connection)

    assert await auth.github_user("42") == "hatchery_1"
    assert await auth.github_user("") is None

    with pytest.raises(auth.GitHubIdentityConflict):
        await auth.save_github_connection("hatchery_2", connection)
    assert await auth.github_user("42") == "hatchery_1"

    await auth.delete_github_connection("hatchery_1")
    assert await auth.github_user("42") is None
    assert "hatchery_1" not in pool.connections
