"""Disk-format compatibility with fx v0.0.8 (session metadata 4, conversation 2).

Fixtures follow src/core/session/{session_codec,session_event,session_log}.zig
at fx tag v0.0.8. No model, sandbox, or fx installation is required.
"""

import json
import os

import pytest

from worker.daemon import main


def _frames(*events, start=1):
    return b"".join(
        (json.dumps({"schema_version": 2, "seq": seq, "timestamp_ms": 123, "event": event}) + "\n").encode()
        for seq, event in enumerate(events, start)
    )


@pytest.fixture
def fx_session(tmp_path, monkeypatch):
    monkeypatch.setenv("FX_HOME", str(tmp_path / ".fx"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = tmp_path / ".fx" / "sessions" / "parent"
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps({
        "schema_version": 4,
        "id": "parent",
        "origin_workspace_root": str(workspace),
        "workspace_root": str(workspace),
        "created_at_ms": 100,
        "updated_at_ms": 100,
        "conversation_language": "en",
        "provider": "gateway",
        "model": "openai/test",
        "effort": "high",
        "fast_mode": False,
        "subagent_child": False,
    }))
    (directory / "events.jsonl").write_bytes(b"")
    return main.Runtime("wrk", str(workspace), None), directory


def test_discovers_v008_session_without_latest_pointer(fx_session):
    runtime, directory = fx_session
    assert not (directory.parent / "latest").exists()
    assert runtime.discover_fx_session(runtime.workspace) == "parent"
    assert runtime.discover_fx_session(runtime.workspace, exclude={"parent"}) is None


@pytest.mark.parametrize("marker", ["metadata", "owner"])
def test_discovery_excludes_children_even_with_newer_activity(fx_session, marker):
    runtime, directory = fx_session
    child = directory.parent / "child"
    child.mkdir()
    metadata = json.loads((directory / "session.json").read_text())
    metadata.update(id="child", updated_at_ms=9_000_000_000_000)
    if marker == "metadata":
        metadata["subagent_child"] = True
    else:
        (child / "subagent").mkdir()
        (child / "subagent" / "owner.json").write_text(json.dumps({"parent_id": "parent"}))
    (child / "session.json").write_text(json.dumps(metadata))
    (child / "events.jsonl").write_bytes(b"")
    assert runtime.discover_fx_session(runtime.workspace) == "parent"


def test_discovery_uses_activity_and_ignores_migrated_pointer(fx_session):
    runtime, directory = fx_session
    old = directory.parent / "old"
    old.mkdir()
    metadata = json.loads((directory / "session.json").read_text())
    metadata.update(id="old", updated_at_ms=200)
    (old / "session.json").write_text(json.dumps(metadata))
    (old / "events.jsonl").write_bytes(b"")
    (directory / "events.jsonl").write_bytes(_frames({"user": {"text": "hi"}}, {"turn_completed": {}}))
    os.utime(old / "events.jsonl", (1, 1))
    os.utime(directory / "events.jsonl", (2, 2))
    latest = directory.parent / "latest"
    latest.mkdir()
    (latest / "stale.json").write_text(json.dumps({
        "workspace_root": runtime.workspace, "session_id": "old", "updated_at_ms": 9_000_000_000_000,
    }))
    assert runtime.discover_fx_session(runtime.workspace) == "parent"


@pytest.mark.parametrize("changes", [{"workspace_root": "/elsewhere"}, {"id": "other"}, {"schema_version": 99}])
def test_discovery_rejects_unrelated_or_unsupported_metadata(fx_session, changes):
    runtime, directory = fx_session
    path = directory / "session.json"
    metadata = json.loads(path.read_text())
    path.write_text(json.dumps(metadata | changes))
    assert runtime.discover_fx_session(runtime.workspace) is None


def test_decodes_conversation_and_external_tool_output(fx_session):
    runtime, directory = fx_session
    (directory / "tool-results").mkdir()
    (directory / "tool-results" / "result-shell.txt").write_text("full tool output")
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"assistant": {"text": "Checking."}},
        {"tool_call": {"call_id": "call_1", "tool_name": "shell", "arguments_json": '{"command":"pwd"}'}},
        {"tool_result": {"call_id": "call_1", "tool_name": "shell", "status": "success", "artifact_ref": "result-shell.txt", "stored_bytes": 16, "completeness": "complete", "preview": "full"}},
        {"steering": {"text": "also inspect tests"}},
        {"assistant": {"text": "Done."}},
        {"turn_completed": {}},
    ))
    events = list(runtime.stream_fx_events(runtime.workspace, wait=False))
    assert [event["type"] for event in events] == ["user", "assistant", "tool.call", "tool.result", "user", "assistant", "turn.completed"]
    assert events[2]["tool"] == {"name": "shell", "arguments": {"command": "pwd"}}
    assert events[3]["output"] == "full tool output"
    assert events[3]["error"] is False
    assert events[4]["text"] == "also inspect tests"
    assert all(event["session_id"] == "parent" for event in events)


@pytest.mark.parametrize("reference", ["missing.txt", "../secret", "/tmp/outside-fx", "symlink.txt", "wrong-size.txt"])
def test_artifact_fallback_never_reads_outside_session(fx_session, reference):
    runtime, directory = fx_session
    (directory / "tool-results").mkdir()
    secret = directory / "secret"
    secret.write_text("private data")
    (directory / "tool-results" / "symlink.txt").symlink_to(secret)
    (directory / "tool-results" / "wrong-size.txt").write_text("unexpected content")
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"tool_result": {"call_id": "call_1", "status": "failure", "artifact_ref": reference, "stored_bytes": 999, "preview": "safe preview"}},
        {"turn_completed": {}},
    ))
    result = list(runtime.stream_fx_events(runtime.workspace, wait=False))[1]
    assert result["output"] == "safe preview"
    assert result["error"] is True
    assert main.Runtime.transcript_payload(result)["truncated"] is True


def test_artifact_output_stays_bounded(fx_session):
    runtime, directory = fx_session
    (directory / "tool-results").mkdir()
    (directory / "tool-results" / "large.txt").write_text("x" * 20_000)
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"tool_result": {"call_id": "call_1", "status": "success", "artifact_ref": "large.txt", "stored_bytes": 20_000}},
        {"turn_completed": {}},
    ))
    result = list(runtime.stream_fx_events(runtime.workspace, wait=False))[1]
    assert len(result["output"]) == 8193
    payload = main.Runtime.transcript_payload(result)
    assert len(payload["output"]) == 8192
    assert payload["truncated"] is True


def test_partial_lines_and_unfinished_turn_are_retried(fx_session, monkeypatch):
    runtime, directory = fx_session
    complete = _frames({"user": {"text": "hello"}}, {"assistant": {"text": "hi"}}, {"turn_completed": {}})
    path = directory / "events.jsonl"
    path.write_bytes(complete[:-3])
    assert list(runtime.stream_fx_events(runtime.workspace, wait=False)) == []
    monkeypatch.setattr(main.time, "sleep", lambda _: path.write_bytes(complete))
    stream = runtime.stream_fx_events(runtime.workspace)
    try:
        assert [next(stream)["type"] for _ in range(3)] == ["user", "assistant", "turn.completed"]
    finally:
        stream.close()


def test_malformed_complete_record_is_not_silently_discarded():
    with pytest.raises(json.JSONDecodeError):
        main.Runtime.decode_fx_jsonl(b'{"schema_version":2,bad}\n')


def test_non_object_tool_arguments_are_preserved():
    raw = _frames(
        {"user": {"text": "inspect"}},
        {"tool_call": {"call_id": "call_1", "tool_name": "read_file", "arguments_json": "[]"}},
        {"turn_completed": {}},
    )
    events = main.Runtime.decode_fx_jsonl(raw)
    assert events[1]["tool"]["arguments"] == {"raw": "[]"}


def test_live_recovery_tools_are_not_repeated_when_turn_commits(fx_session, monkeypatch):
    runtime, directory = fx_session
    recovery = directory / "recovery.json"
    recovery.write_text(json.dumps({
        "conversation_seq": 0,
        "checkpoint": {
            "version": 2, "turn_id": 42, "user": {"text": "inspect"},
            "assistant_source": "", "execution": {"tool_steps": [{
                "tool_calls": [{"id": "call_1", "name": "shell", "arguments_json": "{}"}],
                "tool_results": [{"tool_call_id": "call_1", "status": "success", "output": "pwd output"}],
            }]},
        },
    }))
    raw = _frames(
        {"user": {"text": "inspect"}},
        {"tool_call": {"call_id": "call_1", "tool_name": "shell", "arguments_json": "{}"}},
        {"tool_result": {"call_id": "call_1", "status": "success", "preview": "pwd output"}},
        {"assistant": {"text": "done"}},
        {"turn_completed": {}},
    )
    # Leave the stale recovery sidecar in place to exercise the commit race.
    monkeypatch.setattr(main.time, "sleep", lambda _: (directory / "events.jsonl").write_bytes(raw))
    stream = runtime.stream_fx_events(runtime.workspace, stop=lambda: (directory / "events.jsonl").stat().st_size > 0)
    events = list(stream)
    assert [event["type"] for event in events] == ["user", "tool.call", "tool.result", "assistant", "turn.completed"]


def test_context_checkpoint_is_not_completion_and_keeps_open_user(fx_session):
    runtime, directory = fx_session
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"assistant": {"text": "working"}},
        {"context_checkpoint": {"covers_through_seq": 2, "summary": "context"}},
    ))
    (directory / "recovery.json").write_text(json.dumps({
        "conversation_seq": 3,
        "checkpoint": {"turn_id": 42, "user": {"text": "inspect"}, "execution": {}},
    }))
    events = list(runtime.stream_fx_events(runtime.workspace, wait=False))
    assert [event["type"] for event in events] == ["user", "assistant"]


@pytest.mark.parametrize("replace", [False, True])
def test_stream_survives_replaced_or_truncated_suffix(fx_session, monkeypatch, replace):
    runtime, directory = fx_session
    path = directory / "events.jsonl"
    first = _frames({"user": {"text": "first"}}, {"assistant": {"text": "first answer"}}, {"turn_completed": {}})
    second = _frames({"user": {"text": "second"}}, {"assistant": {"text": "second answer"}}, {"turn_completed": {}}, start=4)
    path.write_bytes(first + b'{"incomplete":')

    def rewrite(_):
        if replace:
            temporary = directory / "events.tmp"
            temporary.write_bytes(first + second)
            temporary.replace(path)
        else:
            path.write_bytes(first + second)

    monkeypatch.setattr(main.time, "sleep", rewrite)
    stream = runtime.stream_fx_events(runtime.workspace, stop=lambda: path.read_bytes().endswith(second))
    events = list(stream)
    assert [event["text"] for event in events if event["type"] == "user"] == ["first", "second"]
    assert len([event for event in events if event["type"] == "turn.completed"]) == 2


@pytest.mark.parametrize("reason,expected", [("cancelled", "task.question"), ("failed", "task.failed")])
async def test_interrupted_turn_is_never_reported_as_success(fx_session, reason, expected):
    runtime, directory = fx_session
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"interrupted": {"reason": reason, "partial_text": "unfinished"}},
    ))
    emitted = []

    async def publish(event):
        emitted.append(event)

    runtime.publish = publish

    class ExitedSession:
        exit_code = 0

        def wait(self):
            return 0

    await runtime._stream_task("task", ExitedSession())
    assert [event["type"] for event in emitted] == ["task.transcript", "task.output", expected]


async def test_disk_events_feed_existing_task_and_telemetry_envelopes(fx_session):
    runtime, directory = fx_session
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"tool_call": {"call_id": "call_1", "tool_name": "read_file", "arguments_json": '{"path":"README.md"}'}},
        {"tool_result": {"call_id": "call_1", "status": "success", "preview": "readme contents"}},
        {"assistant": {"text": "done"}},
        {"turn_completed": {}},
    ))
    emitted = []

    async def publish(event):
        emitted.append(event)

    runtime.publish = publish
    runtime.active["task"] = {"model": "test"}

    class ExitedSession:
        exit_code = 0

        def wait(self):
            return 0

    await runtime._stream_task("task", ExitedSession())
    assert [event["type"] for event in emitted] == ["task.transcript", "task.transcript", "task.transcript", "task.output", "task.completed"]
    assert emitted[1]["payload"] == {
        "kind": "tool.call", "session_id": "parent", "tool_call_id": "call_1",
        "tool_name": "read_file", "arguments": '{"path": "README.md"}', "truncated": False,
    }
    assert emitted[2]["payload"]["output"] == "readme contents"
    assert emitted[-1]["payload"]["result"] == {"summary": "done", "session_id": "parent", "tool_calls": []}
    assert [event["sequence"] for event in emitted] == list(range(5))
    assert "task" not in runtime.active


def test_discovery_does_not_rank_uncommitted_activity_as_latest(fx_session):
    runtime, directory = fx_session
    other = directory.parent / "other"
    other.mkdir()
    metadata = json.loads((directory / "session.json").read_text())
    metadata.update(id="other", updated_at_ms=200)
    (other / "session.json").write_text(json.dumps(metadata))
    (other / "events.jsonl").write_bytes(b"")
    (directory / "events.jsonl").write_bytes(_frames({"user": {"text": "not committed"}}))
    (directory / "recovery.json").write_text('{"conversation_seq":0,"checkpoint":{}}')
    assert runtime.discover_fx_session(runtime.workspace) == "other"


def test_stream_stays_with_discovered_session_before_first_turn(fx_session, monkeypatch):
    runtime, directory = fx_session
    raw = _frames({"user": {"text": "ours"}}, {"turn_completed": {}})

    def write_sessions(_):
        other = directory.parent / "other"
        other.mkdir(exist_ok=True)
        metadata = json.loads((directory / "session.json").read_text())
        metadata.update(id="other", updated_at_ms=9_000_000_000_000)
        (other / "session.json").write_text(json.dumps(metadata))
        (other / "events.jsonl").write_bytes(_frames({"user": {"text": "not ours"}}, {"turn_completed": {}}))
        (directory / "events.jsonl").write_bytes(raw)

    monkeypatch.setattr(main.time, "sleep", write_sessions)
    events = list(runtime.stream_fx_events(runtime.workspace, stop=lambda: (directory / "events.jsonl").read_bytes() == raw))
    assert [event["text"] for event in events if event["type"] == "user"] == ["ours"]
    assert all(event["session_id"] == "parent" for event in events)


def test_recovery_user_survives_log_suffix_replacement_without_duplicate(fx_session, monkeypatch):
    runtime, directory = fx_session
    path = directory / "events.jsonl"
    path.write_bytes(b'{"old partial suffix"')
    (directory / "recovery.json").write_text(json.dumps({
        "conversation_seq": 0,
        "checkpoint": {"turn_id": 42, "user": {"text": "inspect"}, "execution": {}},
    }))
    raw = _frames({"user": {"text": "inspect"}}, {"turn_completed": {}})
    monkeypatch.setattr(main.time, "sleep", lambda _: path.write_bytes(raw))
    events = list(runtime.stream_fx_events(runtime.workspace, stop=lambda: path.read_bytes() == raw))
    assert [event["type"] for event in events] == ["user", "turn.completed"]


@pytest.mark.parametrize("sidecar", [False, True])
def test_partial_artifact_is_marked_truncated(fx_session, sidecar):
    runtime, directory = fx_session
    (directory / "tool-results").mkdir()
    (directory / "tool-results" / "partial.txt").write_text("part")
    if sidecar:
        (directory / "recovery.json").write_text(json.dumps({
            "conversation_seq": 0,
            "checkpoint": {"turn_id": 42, "user": {"text": "inspect"}, "execution": {"tool_steps": [{
                "tool_results": [{"tool_call_id": "call_1", "status": "success", "output_handle": "partial.txt", "stored_output_bytes": 4, "truncated": True}],
            }]}},
        }))
    else:
        (directory / "events.jsonl").write_bytes(_frames(
            {"user": {"text": "inspect"}},
            {"tool_result": {"call_id": "call_1", "status": "success", "artifact_ref": "partial.txt", "stored_bytes": 4, "output_bytes": 100, "completeness": "partial"}},
            {"turn_completed": {}},
        ))
    events = list(runtime.stream_fx_events(runtime.workspace, wait=False))
    result = next(event for event in events if event["type"] == "tool.result")
    assert result["output"] == "part"
    assert main.Runtime.transcript_payload(result)["truncated"] is True


@pytest.mark.parametrize("unsafe", ["directory_symlink", "hardlink", "fifo"])
def test_artifact_cannot_escape_through_links_or_block_on_fifo(fx_session, unsafe):
    runtime, directory = fx_session
    outside = directory.parent / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("private data")
    results = directory / "tool-results"
    if unsafe == "directory_symlink":
        results.symlink_to(outside, target_is_directory=True)
    else:
        results.mkdir()
        if unsafe == "hardlink":
            os.link(outside / "result.txt", results / "result.txt")
        else:
            os.mkfifo(results / "result.txt")
    (directory / "events.jsonl").write_bytes(_frames(
        {"user": {"text": "inspect"}},
        {"tool_result": {"call_id": "call_1", "status": "success", "artifact_ref": "result.txt", "preview": "safe preview"}},
        {"turn_completed": {}},
    ))
    events = list(runtime.stream_fx_events(runtime.workspace, wait=False))
    assert events[1]["output"] == "safe preview"
    assert events[1]["truncated"] is True


@pytest.mark.parametrize("record", [
    {"schema_version": 1, "seq": 1, "kind": "history_turn_committed", "payload": {}},
    {"schema_version": 1, "seq": 1, "event": {"turn_completed": {}}},
    {"schema_version": 3, "seq": 1, "event": {"turn_completed": {}}},
])
def test_rejects_records_outside_pinned_conversation_format(record):
    with pytest.raises(ValueError, match="fx 0.0.8"):
        main.Runtime.decode_fx_jsonl((json.dumps(record) + "\n").encode())
