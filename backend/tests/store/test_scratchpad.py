import asyncio

import models
from store import scratchpad


USER = models.ScratchpadActor(kind="user", id="user_1", name="Ada")


async def test_scratchpad_keeps_immutable_versions_and_read_cursor():
    initial = await scratchpad.get()
    assert initial is not None
    assert (initial.version, initial.content) == (0, "")

    first = await scratchpad.write("one", 0, USER)
    second = await scratchpad.write("two", 1, USER)

    assert (first.version, second.version) == (1, 2)
    assert (await scratchpad.get(1)).content == "one"
    assert (await scratchpad.get()).content == "two"
    assert [item.version for item in await scratchpad.list_versions()] == [2, 1, 0]
    assert await scratchpad.mark_read(2) == 2
    assert await scratchpad.mark_read(1) == 2
    assert await scratchpad.state() == (2, 2)


async def test_scratchpad_compare_and_swap_allows_one_writer():
    async def save(content):
        try:
            return await scratchpad.write(content, 0, USER)
        except scratchpad.VersionConflict as error:
            return error

    results = await asyncio.gather(save("a"), save("b"))

    assert sum(isinstance(result, models.ScratchpadVersion) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, scratchpad.VersionConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].current_version == 1


async def test_restoring_content_appends_instead_of_rewriting():
    await scratchpad.write("first", 0, USER)
    await scratchpad.write("second", 1, USER)
    restored = await scratchpad.write("first", 2, USER)

    assert restored.version == 3
    assert (await scratchpad.get(1)).content == "first"
    assert (await scratchpad.get(2)).content == "second"


def test_structured_diff_has_line_numbers_and_is_bounded():
    lines, truncated = scratchpad.structured_diff("same\nold", "same\nnew")

    assert truncated is False
    assert [(line.kind, line.text) for line in lines] == [
        ("header", "@@ -1,2 +1,2 @@"),
        ("context", "same"),
        ("remove", "old"),
        ("add", "new"),
    ]
    assert lines[2].old_line == 2
    assert lines[3].new_line == 2
