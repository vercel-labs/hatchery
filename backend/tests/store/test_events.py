from store import events


async def test_append_read_tail():
    assert await events.tail_index("c1") == -1
    assert await events.append("c1", {"type": "a"}) == 0
    assert await events.append("c1", {"type": "b"}) == 1
    assert await events.tail_index("c1") == 1
    assert await events.read("c1") == [(0, {"type": "a"}), (1, {"type": "b"})]
    assert await events.read("c1", 1) == [(1, {"type": "b"})]


async def test_streams_are_isolated():
    await events.append("c1", {"n": 1})
    assert await events.read("c2") == []
    assert await events.tail_index("c2") == -1
