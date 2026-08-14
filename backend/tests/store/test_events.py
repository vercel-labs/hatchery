from store import events


async def test_append_read_tail():
    assert await events.tail("c1", "messages") is None
    assert await events.append("c1", "messages", {"type": "a"}) == 0
    assert await events.append("c1", "messages", {"type": "b"}) == 1
    assert await events.read("c1", "messages") == [(0, {"type": "a"}), (1, {"type": "b"})]
    assert await events.read("c1", "messages", 1) == [(1, {"type": "b"})]
    assert await events.tail("c1", "messages") == {"type": "b"}


async def test_streams_and_namespaces_are_isolated():
    await events.append("c1", "messages", {"n": 1})
    await events.append("c1", "worker", {"box": "b1"})
    assert await events.read("c2", "messages") == []
    assert await events.read("c1", "messages") == [(0, {"n": 1})]
    assert await events.tail("c1", "worker") == {"box": "b1"}
