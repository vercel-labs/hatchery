from store import terminals


async def test_terminals_are_owned_and_ordered_per_chat():
    first = await terminals.create("chat_1", "devbox_1", "bash")
    second = await terminals.create("chat_1", "devbox_1", "bash 2")
    await terminals.create("chat_2", "devbox_2", "other")

    first["session_id"] = "session_1"
    first["state"] = "running"
    await terminals.save(first)

    listed = await terminals.list_for_chat("chat_1")
    assert [record["id"] for record in listed] == [first["id"], second["id"]]
    assert listed[0]["session_id"] == "session_1"
    assert await terminals.get(first["id"]) == listed[0]


async def test_terminals_can_be_deleted_individually_or_by_devbox():
    first = await terminals.create("chat_1", "devbox_1", "bash")
    second = await terminals.create("chat_1", "devbox_1", "bash 2")
    other = await terminals.create("chat_1", "devbox_2", "other")

    assert await terminals.delete(first["id"]) is True
    assert await terminals.delete(first["id"]) is False
    await terminals.delete_for_devbox("devbox_1")

    assert await terminals.get(second["id"]) is None
    assert await terminals.get(other["id"]) == other
