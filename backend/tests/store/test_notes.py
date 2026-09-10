import asyncio
import threading

import pytest

from store import notes, spaces


async def test_notes_are_scoped_sorted_and_revision_checked():
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



async def test_find_replace_accepts_one_exact_or_high_confidence_match():
    space = await spaces.create("edits")
    note = await notes.create(
        space.id,
        "decisions.md",
        "## Decisions\n\n- Keep notes lean and durable.\n\n## Links\n",
    )

    exact = await notes.find_replace(
        space.id,
        note.filename,
        "## Links",
        "## References",
    )
    assert exact is not None
    exact_note, match, score = exact
    assert (match, score) == ("exact", 1.0)
    assert "## References" in exact_note.content

    fuzzy = await notes.find_replace(
        space.id,
        note.filename,
        "- Keep note lean and durable.\n",
        "- Keep notes concise and durable.\n",
    )
    assert fuzzy is not None
    fuzzy_note, match, score = fuzzy
    assert match == "fuzzy"
    assert score >= notes.FUZZY_MATCH_THRESHOLD
    assert "- Keep notes concise and durable.\n" in fuzzy_note.content
    assert fuzzy_note.revision == 3


async def test_find_replace_rejects_missing_ambiguous_and_low_confidence_text():
    space = await spaces.create("safe edits")
    note = await notes.create(
        space.id,
        "facts.md",
        "Repeated durable context.\nRepeated durable context.\nA distinct final line.\n",
    )

    with pytest.raises(notes.FindReplaceError, match="ambiguous"):
        await notes.find_replace(
            space.id, note.filename, "Repeated durable context.", "replacement"
        )
    with pytest.raises(notes.FindReplaceError, match="ambiguous"):
        notes._find_replace("###", "##", "changed")
    with pytest.raises(notes.FindReplaceError) as short:
        await notes.find_replace(space.id, note.filename, "missing", "replacement")
    assert short.value.reason == "low_confidence"
    with pytest.raises(notes.FindReplaceError) as missing:
        notes._find_replace("", "a long missing line\n", "replacement\n")
    assert missing.value.reason == "missing"
    with pytest.raises(notes.FindReplaceError) as weak:
        await notes.find_replace(
            space.id,
            note.filename,
            "Completely unrelated durable sentence.\n",
            "replacement\n",
        )
    assert weak.value.reason == "low_confidence"
    assert weak.value.score is not None
    assert (await notes.get(space.id, note.filename)).revision == 1


async def test_find_replace_rejects_an_ambiguous_fuzzy_match():
    space = await spaces.create("ambiguous")
    note = await notes.create(
        space.id,
        "facts.md",
        "- Durable context belongs here.\n- Durable contexts belong here.\n",
    )

    with pytest.raises(notes.FindReplaceError) as ambiguous:
        await notes.find_replace(
            space.id,
            note.filename,
            "- Durable context belong here.\n",
            "replacement\n",
        )
    assert ambiguous.value.reason == "ambiguous"
    assert (await notes.get(space.id, note.filename)).revision == 1


async def test_override_revision_must_still_be_current_when_locked():
    space = await spaces.create("concurrent")
    captured = await notes.create(space.id, "facts.md", "old durable context\n")

    agent = await notes.find_replace(
        space.id, captured.filename, "old durable context", "new durable context"
    )
    assert agent is not None
    with pytest.raises(notes.NoteConflict) as first_conflict:
        await notes.update(
            space.id, captured.filename, "human draft\n", captured.revision
        )
    reviewed_revision = first_conflict.value.current.revision
    assert first_conflict.value.current.content == "new durable context\n"

    intervening = await notes.find_replace(
        space.id, captured.filename, "new durable context", "newer durable context"
    )
    assert intervening is not None
    with pytest.raises(notes.NoteConflict) as second_conflict:
        await notes.update(
            space.id, captured.filename, "human draft\n", reviewed_revision
        )
    assert second_conflict.value.current.content == "newer durable context\n"

    saved = await notes.update(
        space.id,
        captured.filename,
        "human draft\n",
        second_conflict.value.current.revision,
    )
    assert saved is not None
    assert saved.content == "human draft\n"
    assert saved.revision == 4


async def test_agent_and_human_edits_share_one_local_note_lock(monkeypatch):
    space = await spaces.create("simultaneous")
    captured = await notes.create(space.id, "facts.md", "old durable context\n")
    matching = threading.Event()
    release = threading.Event()
    original = notes._find_replace

    def paused_match(content, find, replacement):
        matching.set()
        assert release.wait(timeout=5)
        return original(content, find, replacement)

    monkeypatch.setattr(notes, "_find_replace", paused_match)
    agent = asyncio.create_task(
        asyncio.to_thread(
            asyncio.run,
            notes.find_replace(
                space.id,
                captured.filename,
                "old durable context",
                "agent durable context",
            ),
        )
    )
    assert await asyncio.to_thread(matching.wait, 5)
    human = asyncio.create_task(
        asyncio.to_thread(
            asyncio.run,
            notes.update(
                space.id,
                captured.filename,
                "human durable context\n",
                captured.revision,
            ),
        )
    )
    release.set()

    assert await agent is not None
    with pytest.raises(notes.NoteConflict):
        await human
    current = await notes.get(space.id, captured.filename)
    assert current is not None
    assert current.content == "agent durable context\n"
    assert current.revision == 2


async def test_note_create_rejects_duplicates_and_invalid_values(monkeypatch):
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
    assert notes.MAX_CONTENT_LENGTH == 9_007_199_254_740_991
    large = await notes.create(space.id, "large.md", "x" * 32_001)
    assert len(large.content) == 32_001

    monkeypatch.setattr(notes, "MAX_CONTENT_LENGTH", 32_001)
    with pytest.raises(ValueError, match="at most 32001"):
        await notes.create(space.id, "too_large.md", "x" * 32_002)


async def test_note_count_is_bounded(monkeypatch):
    space = await spaces.create("bounded")
    monkeypatch.setattr(notes, "MAX_NOTES_PER_SPACE", 1)
    await notes.create(space.id, "first.md")

    with pytest.raises(notes.NoteLimitReached):
        await notes.create(space.id, "second.md")


async def test_delete_removes_one_note_and_delete_for_space_removes_the_rest():
    first = await spaces.create("first")
    second = await spaces.create("second")
    await notes.create(first.id, "foo.md", "first")
    await notes.create(first.id, "bar.md", "first")
    await notes.create(second.id, "foo.md", "second")

    assert await notes.delete(first.id, "foo.md") is True
    assert await notes.delete(first.id, "foo.md") is False
    assert await notes.get(first.id, "foo.md") is None

    await notes.delete_for_space(first.id)

    assert await notes.list_for_space(first.id) == []
    assert (await notes.get(second.id, "foo.md")).content == "second"
