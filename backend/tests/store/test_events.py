import asyncio

from store import events


async def test_append_read_tail():
    assert await events.tail("c1", "messages") is None
    assert await events.append("c1", "messages", {"type": "a"}) == 0
    assert await events.append("c1", "messages", {"type": "b"}) == 1
    assert await events.read("c1", "messages") == [(0, {"type": "a"}), (1, {"type": "b"})]
    assert await events.read("c1", "messages", 1) == [(1, {"type": "b"})]
    assert await events.tail("c1", "messages") == {"type": "b"}


async def test_watch_replays_and_receives_new_events():
    await events.append("c1", "ui", {"type": "old"})
    watcher = events.watch("c1", "ui")
    assert await anext(watcher) == (0, {"type": "old"})

    waiting = asyncio.create_task(anext(watcher))
    await asyncio.sleep(0)
    await events.append("c1", "ui", {"type": "new"})
    assert await waiting == (1, {"type": "new"})
    await watcher.aclose()


async def test_streams_and_namespaces_are_isolated():
    await events.append("c1", "messages", {"n": 1})
    await events.append("c1", "worker", {"box": "b1"})
    assert await events.read("c2", "messages") == []
    assert await events.read("c1", "messages") == [(0, {"n": 1})]
    assert await events.tail("c1", "worker") == {"box": "b1"}
