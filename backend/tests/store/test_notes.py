import pytest

from store import notes, spaces


async def test_notes_are_scoped_sorted_and_editable():
    first = await spaces.create("first")
    second = await spaces.create("second")

    reviewed = await notes.create(first.id, "reviewed_issues.md", "# Reviewed\n")
    foo = await notes.create(first.id, "foo.md")
    await notes.create(second.id, "foo.md", "other space")
    updated = await notes.update(first.id, "foo.md", "one durable fact", foo.revision)

    assert reviewed.content == "# Reviewed\n"
    assert foo.content == ""
    assert updated is not None and updated.content == "one durable fact"
    assert updated.revision == 2
    assert [note.filename for note in await notes.list_for_space(first.id)] == [
        "foo.md",
        "reviewed_issues.md",
    ]
    assert (await notes.get(second.id, "foo.md")).content == "other space"
    assert [summary.filename for summary in await notes.list_summaries(first.id)] == [
        "foo.md",
        "reviewed_issues.md",
    ]
    assert await notes.get(first.id, "missing.md") is None
    assert await notes.update(first.id, "missing.md", "no", 1) is None
    with pytest.raises(notes.NoteConflict) as conflict:
        await notes.update(first.id, "foo.md", "stale", foo.revision)
    assert conflict.value.current.content == "one durable fact"


async def test_note_create_rejects_duplicates_and_invalid_values():
    space = await spaces.create("notes")
    await notes.create(space.id, "foo.md")

    with pytest.raises(notes.NoteExists):
        await notes.create(space.id, "foo.md")
    for filename in (
        "foo",
        "../foo.md",
        "nested/foo.md",
        ".hidden.md",
        "two words.md",
        "résumé.md",
    ):
        with pytest.raises(ValueError, match="simple .md name"):
            await notes.create(space.id, filename)
    with pytest.raises(ValueError, match="at most 32000"):
        await notes.create(space.id, "large.md", "x" * 32_001)


async def test_note_count_is_bounded(monkeypatch):
    space = await spaces.create("bounded")
    monkeypatch.setattr(notes, "MAX_NOTES_PER_SPACE", 1)
    await notes.create(space.id, "first.md")

    with pytest.raises(notes.NoteLimitReached):
        await notes.create(space.id, "second.md")


async def test_delete_for_space_removes_only_its_notes():
    first = await spaces.create("first")
    second = await spaces.create("second")
    await notes.create(first.id, "foo.md", "first")
    await notes.create(second.id, "foo.md", "second")

    await notes.delete_for_space(first.id)

    assert await notes.list_for_space(first.id) == []
    assert (await notes.get(second.id, "foo.md")).content == "second"
