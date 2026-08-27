from store import devboxes


async def test_devboxes_are_owned_and_ordered_per_chat():
    first = await devboxes.create("chat_1", "main", ["a/b"])
    second = await devboxes.create("chat_1", "scratch", [])
    await devboxes.create("chat_2", "other", ["c/d"])

    first["state"] = "ready"
    first["box"] = {"id": "box_1", "url": "https://box.example"}
    await devboxes.save(first)

    listed = await devboxes.list_for_chat("chat_1")
    assert [record["id"] for record in listed] == [first["id"], second["id"]]
    assert listed[0]["repos"] == ["a/b"]
    assert listed[0]["box"]["id"] == "box_1"
    assert await devboxes.get(first["id"]) == listed[0]


async def test_devbox_can_be_deleted():
    record = await devboxes.create("chat_1", "main", [])

    assert await devboxes.delete(record["id"]) is True
    assert await devboxes.delete(record["id"]) is False
    assert await devboxes.get(record["id"]) is None
