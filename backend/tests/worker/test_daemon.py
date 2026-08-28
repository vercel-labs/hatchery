import asyncio
import json
import threading
import urllib.error
import urllib.request

from worker.daemon import main


def test_health_is_authenticated(monkeypatch):
    monkeypatch.setenv("HATCHERY_DAEMON_TOKEN", "secret")
    server = main.http.server.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/health"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"authorization": "Bearer secret"})
        ) as response:
            assert response.read() == b'{"ok": true, "version": 3}'
        try:
            urllib.request.urlopen(url)
        except urllib.error.HTTPError as error:
            assert error.code == 401
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


async def test_runtime_runs_fx_and_resumes_follow_up(monkeypatch, tmp_path):
    emitted = []
    commands = []

    class Session:
        exit_code = 0

        def __init__(self, task_id, command, workspace, cols, rows, env):
            commands.append((command, {"cwd": workspace, "env": env}))

        def wait(self):
            return 0

        def read(self, offset, timeout=25):
            return 0, json.dumps({"output": "done", "session_id": "ses_1"}).encode(), 0

        def send_signal(self, sent):
            pass

    monkeypatch.setattr(main, "TTYSession", Session)

    async def publish(event):
        emitted.append(event)

    runtime = main.Runtime("wrk_1", str(tmp_path), publish)
    base = {
        "worker_id": "wrk_1",
        "task_id": "task_1",
        "payload": {"prompt": "fix it", "model": "openai/test"},
    }
    await runtime.handle({**base, "sequence": 0, "type": "task.launch"})
    await asyncio.gather(*runtime.jobs)
    await runtime.handle({**base, "sequence": 1, "type": "task.input"})
    await asyncio.gather(*runtime.jobs)

    assert "--resume" not in commands[0][0]
    assert commands[1][0][0:7] == ["fx", "ask", "--json", "--yolo", "--resume", "last", "--"]
    assert commands[0][1]["cwd"] == str(tmp_path)
    assert commands[0][1]["env"]["FX_MODEL"] == "openai/test"
    assert [event["type"] for event in emitted] == [
        "task.started", "task.output", "task.completed",
        "task.started", "task.output", "task.completed",
    ]
    assert [event["sequence"] for event in emitted] == list(range(6))


async def test_runtime_keeps_ordering_state_across_restart(tmp_path):
    state = tmp_path / "state.json"

    async def publish(event):
        pass

    first = main.Runtime("wrk_1", str(tmp_path), publish, str(state))
    first.sequences["task_1"] = 3
    first.event_sequences["task_1"] = 5
    first._save_state()

    restored = main.Runtime("wrk_1", str(tmp_path), publish, str(state))
    assert restored.sequences == {"task_1": 3}
    assert restored.event_sequences == {"task_1": 5}


def test_tty_session_replays_output_and_supports_multiple_viewers(tmp_path):
    session = main.TTYSession(
        "tty_1",
        ["/bin/sh", "-lc", "printf hello"],
        str(tmp_path),
        80,
        24,
    )
    assert session.wait() == 0
    first_offset, first, first_exit = session.read(0, 0)
    second_offset, second, second_exit = session.read(0, 0)
    assert first_offset == second_offset == 0
    assert first == second == b"hello"
    assert first_exit == second_exit == 0


def test_tty_session_accepts_input_resize_and_signal(tmp_path):
    session = main.TTYSession("tty_2", ["/bin/cat"], str(tmp_path), 80, 24)
    session.resize(100, 30)
    session.write(b"hello\n")
    offset, output, exit_code = session.read(0, 2)
    assert offset == 0
    assert b"hello" in output
    assert exit_code is None
    session.send_signal("terminate")
    assert session.wait() < 0
