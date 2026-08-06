"""Sessions and the store that owns them.

A session is one durable conversation: one per slack thread, one per github
issue/pr. The continuation token is the channel-local address of that
conversation, namespaced as "<channel>:<local token>". Single-owner: one live
session per token, enforced by an atomic claim in the store.

Unlike eve (where a session *is* a workflow run), sessions here live in a
Store so the module stays independent of any execution engine.
"""

import asyncio
import datetime
import typing
import uuid

import pydantic

from chat import protocol

DEDUPE_CAP = 10_000


class Session(pydantic.BaseModel):
    id: str
    token: str
    channel: str
    history: list[protocol.Message] = []
    channel_state: dict = {}
    created_at: str


class Store(typing.Protocol):
    async def get(self, session_id: str) -> Session | None: ...

    async def put(self, session: Session) -> None: ...

    async def claim(self, token: str, candidate: Session) -> Session:
        """Atomically map a token to its owning session.

        Stores and returns the candidate if the token is unowned, otherwise
        returns the existing owner. This is what stops two concurrent
        webhooks from both creating a session for the same conversation.
        """
        ...

    async def dedupe(self, key: str) -> bool:
        """True the first time a key is seen, False on replays."""
        ...


class MemoryStore:
    """In-process store for tests and local dev."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._tokens: dict[str, str] = {}
        self._seen: dict[str, None] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            return session.model_copy(deep=True) if session else None

    async def put(self, session: Session) -> None:
        async with self._lock:
            self._sessions[session.id] = session.model_copy(deep=True)

    async def claim(self, token: str, candidate: Session) -> Session:
        async with self._lock:
            owner_id = self._tokens.get(token)
            if owner_id is not None and owner_id in self._sessions:
                return self._sessions[owner_id].model_copy(deep=True)
            self._tokens[token] = candidate.id
            self._sessions[candidate.id] = candidate.model_copy(deep=True)
            return candidate.model_copy(deep=True)

    async def dedupe(self, key: str) -> bool:
        async with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            if len(self._seen) > DEDUPE_CAP:
                for stale in list(self._seen)[: DEDUPE_CAP // 2]:
                    del self._seen[stale]
            return True


async def resolve(store: Store, channel: str, local_token: str, state: dict) -> Session:
    """Find or create the session owning a channel-local conversation."""
    token = f"{channel}:{local_token}"
    candidate = Session(
        id=f"ses_{uuid.uuid4().hex}",
        token=token,
        channel=channel,
        channel_state=dict(state),
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    session = await store.claim(token, candidate)
    session.channel_state.update(state)
    return session
