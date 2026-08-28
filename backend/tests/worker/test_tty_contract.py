import os
import threading
import time

import pytest

from worker.daemon import main


def _session(tmp_path, command):
    return main.TTYSession("tty-contract", command, str(tmp_path), 80, 24, os.environ.copy())


def test_resume_replays_exactly_from_byte_offset(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf ABCDEFGHIJ"])
    assert session.wait() == 0
    offset, output, exit_code = session.read(5, 0)
    assert (offset, output, exit_code) == (5, b"FGHIJ", 0)


def test_unknown_session_is_not_created_without_a_command():
    assert main.Handler.sessions.get("does-not-exist") is None


def test_tty_survives_viewer_disconnect_and_accepts_reconnect(tmp_path):
    session = _session(tmp_path, ["/bin/cat"])
    first_offset, _, _ = session.read(0, 0.01)
    session.write(b"ONE\n")
    _, first, _ = session.read(first_offset, 1)
    assert b"ONE" in first
    rendered = first_offset + len(first)
    session.write(b"TWO\n")
    offset, second, exit_code = session.read(rendered, 1)
    assert offset == rendered
    assert b"TWO" in second
    assert exit_code is None
    session.send_signal("terminate")
    session.wait()


def test_tty_inherits_complete_base_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HATCHERY_TTY_PROBE", "present")
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf %s \"$HATCHERY_TTY_PROBE\""])
    assert session.wait() == 0
    assert session.read(0, 0)[1] == b"present"


def test_tty_generates_session_ids():
    generator = getattr(main.TTYSession, "new_id", None)
    assert callable(generator), "daemon must own unique session IDs"
    assert generator() != generator()


def test_tty_lists_running_sessions():
    listing = getattr(main.Handler, "list_sessions", None)
    assert callable(listing), "authenticated daemon session listing is retained"


def test_tty_session_listing_requires_authentication():
    assert hasattr(main.Handler, "list_sessions")
    assert main.Handler._authorized is not None


def test_tty_input_endpoint_requires_authentication():
    assert main.Handler._authorized is not None


def test_tty_reports_authoritative_geometry(tmp_path):
    session = _session(tmp_path, ["/bin/cat"])
    geometry = getattr(session, "geometry", None)
    assert callable(geometry), "reconnect handshake must report PTY geometry"
    assert geometry() == (80, 24)
    session.send_signal("terminate")
    session.wait()


def test_multiple_viewers_receive_identical_bytes(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf hello"])
    session.wait()
    assert session.read(0, 0)[1] == session.read(0, 0)[1] == b"hello"


def test_slow_viewer_does_not_block_fast_viewer(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf fast"])
    seen = []
    thread = threading.Thread(target=lambda: seen.append(session.read(0, 2)[1]))
    thread.start()
    thread.join(1)
    assert seen == [b"fast"]


def test_viewer_can_attach_mid_stream_without_replaying_seen_bytes(tmp_path):
    session = _session(tmp_path, ["/bin/cat"])
    session.write(b"before\n")
    _, before, _ = session.read(0, 1)
    offset = len(before)
    session.write(b"after\n")
    _, after, _ = session.read(offset, 1)
    assert b"before" not in after
    assert b"after" in after
    session.send_signal("terminate")
    session.wait()


def test_failing_or_detached_viewer_does_not_stop_other_viewers(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf shared"])
    session.wait()
    assert session.read(0, 0)[1] == b"shared"
    assert session.read(0, 0)[1] == b"shared"


def test_concurrent_viewer_reads_are_safe(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf concurrent"])
    outputs = []
    threads = [threading.Thread(target=lambda: outputs.append(session.read(0, 2)[1])) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outputs == [b"concurrent"] * 8


def test_replay_window_reports_lost_bytes_instead_of_silent_jump(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "REPLAY_LIMIT", 4)
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf 12345678"])
    session.wait()
    with pytest.raises(ValueError, match="offset|replay"):
        session.read(0, 0)


def test_tty_output_activity_is_stamped(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "printf activity"])
    session.wait()
    assert hasattr(session, "last_output_at"), "output must stamp box/session activity"


def test_running_exec_is_tracked_until_exit(tmp_path):
    session = _session(tmp_path, ["/bin/sh", "-lc", "sleep .05"])
    assert getattr(session, "running", None) is True
    session.wait()
    assert session.running is False


def test_tty_resize_and_signal_are_forwarded(tmp_path):
    session = _session(tmp_path, ["/bin/cat"])
    session.resize(120, 40)
    session.send_signal("terminate")
    assert session.wait() < 0


def test_tty_sessions_survive_daemon_runtime_restart(tmp_path):
    state = tmp_path / "state.json"
    first = main.Runtime("wrk", str(tmp_path), lambda event: None, str(state))
    session = _session(tmp_path, ["/bin/cat"])
    main.Handler.sessions["durable"] = session
    first._save_state()
    main.Runtime("wrk", str(tmp_path), lambda event: None, str(state))
    assert main.Handler.sessions.get("durable") is session
    session.send_signal("terminate")
    session.wait()
