import asyncio

from chat import protocol, session


async def test_resolve_creates_then_reuses():
    store = session.MemoryStore()
    first = await session.resolve(store, "slack", "C1:100.1", {"channel_id": "C1"})
    second = await session.resolve(store, "slack", "C1:100.1", {"user_id": "U2"})
    assert first.id == second.id
    assert first.token == "slack:C1:100.1"
    assert second.channel_state == {"channel_id": "C1", "user_id": "U2"}  # merged


async def test_resolve_separates_tokens_and_channels():
    store = session.MemoryStore()
    a = await session.resolve(store, "slack", "C1:100.1", {})
    b = await session.resolve(store, "slack", "C1:200.2", {})
    c = await session.resolve(store, "github", "C1:100.1", {})
    assert len({a.id, b.id, c.id}) == 3


async def test_claim_is_single_owner_under_concurrency():
    store = session.MemoryStore()
    sessions = await asyncio.gather(*(session.resolve(store, "slack", "C1:1.0", {}) for _ in range(20)))
    assert len({s.id for s in sessions}) == 1


async def test_put_and_get_round_trip_are_copies():
    store = session.MemoryStore()
    sess = await session.resolve(store, "github", "t", {})
    sess.history.append(protocol.Message(role="user", content="hi"))
    await store.put(sess)
    loaded = await store.get(sess.id)
    assert loaded is not None
    assert [m.content for m in loaded.history] == ["hi"]
    loaded.history.append(protocol.Message(role="assistant", content="mutated"))
    again = await store.get(sess.id)
    assert again is not None
    assert len(again.history) == 1  # store hands out copies


async def test_get_unknown_returns_none():
    store = session.MemoryStore()
    assert await store.get("ses_missing") is None


async def test_dedupe():
    store = session.MemoryStore()
    assert await store.dedupe("slack:ev1") is True
    assert await store.dedupe("slack:ev1") is False
    assert await store.dedupe("slack:ev2") is True


async def test_dedupe_evicts_oldest_half_at_cap(monkeypatch):
    monkeypatch.setattr(session, "DEDUPE_CAP", 4)
    store = session.MemoryStore()
    for i in range(5):
        assert await store.dedupe(f"k{i}") is True
    assert await store.dedupe("k0") is True  # evicted, seen as new again
    assert await store.dedupe("k4") is False  # recent half survived
