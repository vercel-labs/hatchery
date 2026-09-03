import asyncio
import json
import pathlib
import threading
import time

import pytest

from worker.daemon import main


FIXTURES = pathlib.Path(__file__).with_name("testdata")


def _require(subject, name):
    value = getattr(subject, name, None)
    assert callable(value), f"contract requires {subject.__name__}.{name}"
    return value


@pytest.mark.parametrize(
    "scenario,capability",
    [
        ("fx_init_merges_yolo_acknowledgement", "configure_fx"),
        ("fx_settings_are_private", "configure_fx"),
        ("fx_default_model_is_preserved", "configure_fx"),
        ("fx_gateway_key_is_configured", "configure_fx"),
        ("fx_task_instructions_are_stored", "configure_fx"),
        ("fx_instructions_reach_non_repo_workspace", "prepare_workspace"),
        ("fx_does_not_dirty_repo_with_agents_md", "prepare_workspace"),
        ("fx_launches_interactive_tui_without_prompt_argv", "fx_command"),
        ("fx_resume_launches_interactive_last_session", "fx_command"),
        ("fx_empty_restore_does_not_submit_prompt", "fx_command"),
        ("fx_session_is_discovered_by_workspace", "discover_fx_session"),
        ("fx_stream_follows_newer_session", "stream_fx_events"),
        ("fx_stream_never_revisits_old_session", "stream_fx_events"),
        ("fx_stream_stays_on_parent_when_child_moves_pointer", "stream_fx_events"),
        ("fx_committed_message_does_not_invent_question", "decode_fx_event"),
        ("fx_mcp_configuration_merges_servers", "configure_fx_mcp"),
        ("fx_disables_header_authenticated_mcp_server", "configure_fx_mcp"),
        ("fx_cancelled_turn_parks_without_restarting", "decode_fx_event"),
    ],
    ids=lambda value: value,
)
def test_fx_runtime_contract(scenario, capability):
    _require(main.Runtime, capability)


def test_fx_session_discovery_skips_pointer_with_null_timestamp(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    latest = tmp_path / ".fx" / "sessions" / "latest"
    latest.mkdir(parents=True)
    (latest / "pointer.json").write_text(json.dumps({
        "workspace_root": str(workspace),
        "session_id": "session-initializing",
        "updated_at_ms": None,
    }))
    runtime = main.Runtime("wrk", str(workspace), lambda event: None)

    assert runtime.discover_fx_session(str(workspace)) is None

    (latest / "pointer.json").write_text(json.dumps({
        "workspace_root": str(workspace),
        "session_id": "session-ready",
        "updated_at_ms": 123,
    }))
    assert runtime.discover_fx_session(str(workspace)) == "session-ready"


def test_fx_gateway_key_is_process_environment_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = main.Runtime("wrk", str(tmp_path), lambda event: None)

    env = runtime.configure_fx(gateway_key="gateway-key")

    assert env == {"AI_GATEWAY_API_KEY": "gateway-key"}
    settings = json.loads((tmp_path / ".fx" / "settings.json").read_text())
    assert "AI_GATEWAY_API_KEY" not in settings


def test_fx_gateway_key_uses_current_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gateway-key")
    runtime = main.Runtime("wrk", str(tmp_path), lambda event: None)

    assert runtime.configure_fx() == {"AI_GATEWAY_API_KEY": "gateway-key"}


@pytest.mark.parametrize(
    "fixture,scenario",
    [
        ("fx_events.jsonl", "decode_events_and_coalesce_checkpoints"),
        ("fx_events.jsonl", "normalize_fx_tool_inputs"),
        ("fx_events.jsonl", "stream_events_with_stable_source_keys"),
        ("fx_user_turn.jsonl", "ingest_user_turn_once_before_agent_work"),
        ("fx_two_turns.jsonl", "ingest_each_typed_submission_once"),
    ],
    ids=lambda value: value,
)
def test_captured_fx_jsonl_contract(fixture, scenario):
    raw = (FIXTURES / fixture).read_bytes()
    assert raw.endswith(b"\n")
    decoder = _require(main.Runtime, "decode_fx_jsonl")
    events = decoder(raw)
    assert events, scenario


def test_transcript_payload_is_bounded_and_flat():
    payload = main.Runtime.transcript_payload(
        {
            "type": "tool.result",
            "id": "call_1",
            "output": "abcdef",
            "error": False,
            "session_id": "session_1",
        },
        max_text=4,
    )

    assert payload == {
        "kind": "tool.result",
        "session_id": "session_1",
        "tool_call_id": "call_1",
        "output": "abcd",
        "error": False,
        "truncated": True,
    }


class _RecordedSession:
    exit_code = None

    def __init__(self):
        self.output = bytearray()
        self.writes = []
        self.condition = threading.Condition()

    def write(self, data):
        self.writes.append(data)


def _delivery(runtime, session, text, *, first=False):
    deliver = _require(runtime, "deliver_input")
    result = deliver(session, text, first=first)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_first_fx_delivery_waits_for_bracketed_paste_readiness():
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    _require(runtime, "deliver_input")
    session = _RecordedSession()
    done = threading.Event()

    def send():
        _delivery(runtime, session, "the task", first=True)
        done.set()

    thread = threading.Thread(target=send)
    thread.start()
    time.sleep(0.03)
    assert session.writes == []
    with session.condition:
        session.output.extend(b"terminal drew something")
        session.condition.notify_all()
    time.sleep(0.03)
    assert session.writes == []
    with session.condition:
        session.output.extend(main.FX_INPUT_READY)
        session.condition.notify_all()
    thread.join(main.FX_SUBMIT_BEAT + 1)
    assert done.is_set()


def test_first_fx_delivery_is_bracketed_paste_then_delayed_enter(monkeypatch):
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", sleeps.append)
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    session = _RecordedSession()
    session.output.extend(main.FX_INPUT_READY)
    _delivery(runtime, session, "first\nsecond", first=True)
    assert session.writes == [b"\x1b[200~first\nsecond\x1b[201~", b"\r"]
    assert sleeps == [main.FX_SUBMIT_BEAT]


def test_follow_up_settles_interrupt_and_paste_before_enter(monkeypatch):
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", sleeps.append)
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    session = _RecordedSession()
    session.output.extend(b"drawn")
    _delivery(runtime, session, "answer", first=False)
    assert session.writes == [b"\x03", b"\x1b[200~answer\x1b[201~", b"\r"]
    assert sleeps == [main.FX_INTERRUPT_SETTLE, main.FX_SUBMIT_BEAT]


def test_empty_fx_input_is_refused():
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    with pytest.raises(ValueError):
        _delivery(runtime, _RecordedSession(), "", first=False)


def test_input_to_exited_fx_session_reports_no_session():
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    session = _RecordedSession()
    session.exit_code = 0
    with pytest.raises(LookupError):
        _delivery(runtime, session, "hello", first=False)


def test_concurrent_fx_deliveries_do_not_interleave(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda delay: None)
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    session = _RecordedSession()
    session.output.extend(b"drawn")
    threads = [threading.Thread(target=_delivery, args=(runtime, session, text), kwargs={"first": False}) for text in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(session.writes) == 6
    assert session.writes[0::3] == [b"\x03", b"\x03"]
    assert session.writes[2::3] == [b"\r", b"\r"]


def test_readiness_wait_is_paid_only_for_first_delivery(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda delay: None)
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    session = _RecordedSession()
    session.output.extend(main.FX_INPUT_READY)
    _delivery(runtime, session, "first", first=True)
    started = time.monotonic()
    _delivery(runtime, session, "second", first=False)
    assert time.monotonic() - started < 0.5


def test_cancelled_caller_never_types_into_fx():
    runtime = main.Runtime("wrk", "/tmp", lambda event: None)
    _require(runtime, "deliver_input")
    assert hasattr(runtime, "cancel_pending_input"), "delivery must stop when its caller is cancelled"
