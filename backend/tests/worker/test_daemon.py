import asyncio
import json
import threading
import urllib.error
import urllib.request

from worker.daemon import main


def test_health_is_authenticated(monkeypatch):
    monkeypatch.setenv("HATCHERY_DAEMON_TOKEN", "secret")
    monkeypatch.setenv("HATCHERY_EVENT_DEPLOYMENT", "dpl_1")
    server = main.http.server.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/health"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"authorization": "Bearer secret"})
        ) as response:
            assert json.loads(response.read()) == {
                "ok": True,
                "version": main.VERSION,
                "queue_connected": False,
                "queue_error": None,
                "event_deployment": "dpl_1",
            }
        try:
            urllib.request.urlopen(url)
        except urllib.error.HTTPError as error:
            assert error.code == 401
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


async def test_runtime_round_trips_commit_signing_over_queue(tmp_path):
    emitted = []

    async def publish(event):
        emitted.append(event)

    runtime = main.Runtime("wrk_1", str(tmp_path), publish)
    runtime.loop = asyncio.get_running_loop()
    result = asyncio.create_task(asyncio.to_thread(
        runtime.request_signing,
        {"repo": {"owner": "acme", "name": "app"}},
        1,
    ))
    while not emitted:
        await asyncio.sleep(0)
    request_id = emitted[0]["payload"]["request_id"]
    await runtime.handle({
        "worker_id": "wrk_1",
        "task_id": None,
        "sequence": 0,
        "type": "sign.completed",
        "payload": {"request_id": request_id, "signed_shas": ["signed"]},
    })

    assert await result == ["signed"]
    assert emitted[0]["type"] == "sign.requested"


async def test_runtime_runs_interactive_fx_and_reuses_it_for_follow_up(monkeypatch, tmp_path):
    emitted = []
    commands = []

    class Session:
        exit_code = None

        def __init__(self, task_id, command, workspace, cols, rows, env):
            commands.append((command, {"cwd": workspace, "env": env}))
            self.output = bytearray(main.FX_INPUT_READY)
            self.writes = []
            self.condition = threading.Condition()

        def write(self, data):
            self.writes.append(data)

        def send_signal(self, sent):
            pass

    monkeypatch.setattr(main, "TTYSession", Session)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gateway-key")

    async def publish(event):
        emitted.append(event)

    runtime = main.Runtime("wrk_1", str(tmp_path), publish)

    async def keep_streaming(task_id, session):
        return None

    monkeypatch.setattr(runtime, "_stream_task", keep_streaming)
    base = {
        "worker_id": "wrk_1",
        "task_id": "task_1",
        "payload": {"prompt": "fix it", "model": "openai/test"},
    }
    await runtime.handle({**base, "sequence": 0, "type": "task.launch"})
    await asyncio.gather(*runtime.jobs)
    session = runtime.processes["task_1"]
    await runtime.handle({**base, "sequence": 1, "type": "task.input"})
    await asyncio.gather(*runtime.jobs)

    assert commands == [(["fx"], {"cwd": str(tmp_path), "env": commands[0][1]["env"]})]
    assert commands[0][1]["env"]["AI_GATEWAY_API_KEY"] == "gateway-key"
    assert json.loads((tmp_path / ".fx" / "settings.json").read_text()) == {
        "permission_mode": "yolo",
        "yolo_acknowledged": True,
        "model": "openai/test",
    }
    assert session.writes == [
        b"\x1b[200~fix it\x1b[201~",
        b"\r",
        b"\x03",
        b"\x1b[200~fix it\x1b[201~",
        b"\r",
    ]
    assert [event["type"] for event in emitted] == ["task.started", "task.started"]
    assert [event["sequence"] for event in emitted] == [0, 1]
    assert main.Handler.sessions["task_1"] is session


async def test_stream_drains_completion_buffered_after_process_exit(monkeypatch, tmp_path):
    emitted = []

    class Session:
        exit_code = None

        def wait(self):
            return 0

    session = Session()

    async def publish(event):
        emitted.append(event)

    runtime = main.Runtime("wrk_1", str(tmp_path), publish)

    def stream(*args, **kwargs):
        yield {"type": "assistant", "text": "done", "session_id": "fx_1"}
        session.exit_code = 0
        yield {"type": "turn.completed", "session_id": "fx_1"}

    monkeypatch.setattr(runtime, "stream_fx_events", stream)
    runtime.processes["task_1"] = session
    runtime.active["task_1"] = {"model": "openai/test"}

    await runtime._stream_task("task_1", session)

    assert [event["type"] for event in emitted] == ["task.output", "task.completed"]
    assert emitted[-1]["payload"]["result"]["summary"] == "done"
    assert "task_1" not in runtime.active


async def test_queue_poll_failure_is_reported_and_retried():
    attempts = 0
    sleeps = []

    class Queue:
        def poll(self, *args, **kwargs):
            async def deliveries():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("tunnel offline")
                raise asyncio.CancelledError
                yield
            return deliveries()

    async def sleep(delay):
        sleeps.append(delay)

    main.Handler.queue_connected = False
    main.Handler.queue_error = None
    try:
        await main.poll_commands(Queue(), object(), "wrk_1", sleep)
    except asyncio.CancelledError:
        pass

    assert attempts == 2
    assert sleeps == [2]
    assert main.Handler.queue_connected is False
    assert main.Handler.queue_error == "RuntimeError: tunnel offline"


async def test_command_is_not_acknowledged_when_process_handoff_fails(monkeypatch, tmp_path):
    emitted = []

    async def publish(event):
        emitted.append(event)

    runtime = main.Runtime("wrk_1", str(tmp_path), publish, str(tmp_path / "state.json"))

    async def launch(*args, **kwargs):
        raise RuntimeError("process failed")

    monkeypatch.setattr(runtime, "_launch", launch)
    command = {
        "worker_id": "wrk_1", "task_id": "task_1", "sequence": 0,
        "type": "task.launch", "payload": {"prompt": "fix it"},
    }

    try:
        await runtime.handle(command)
    except RuntimeError as error:
        assert str(error) == "process failed"
    else:
        raise AssertionError("failed handoff must reject Queue delivery")
    assert runtime.sequences == {}


async def test_runtime_recovers_active_task_with_fx_resume(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"active": {"task_1": {"model": "openai/test"}}}))
    runtime = main.Runtime("wrk_1", str(tmp_path), lambda event: None, str(state))
    calls = []

    async def launch(task_id, prompt, model, *, resume):
        calls.append((task_id, prompt, model, resume))

    monkeypatch.setattr(runtime, "_launch", launch)
    await runtime.recover_active_tasks()

    assert calls == [("task_1", "", "openai/test", True)]


def test_fx_resume_is_only_used_for_relaunch():
    assert main.Runtime.fx_command(resume=False) == ["fx"]
    assert main.Runtime.fx_command(resume=True) == ["fx", "--resume", "last"]


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


def test_tty_session_retries_partial_writes(monkeypatch):
    writes = []

    def write(fd, data):
        chunk = bytes(data[:2])
        writes.append((fd, chunk))
        return len(chunk)

    monkeypatch.setattr(main.os, "write", write)
    session = object.__new__(main.TTYSession)
    session.fd = 7

    session.write(b"hello")

    assert writes == [(7, b"he"), (7, b"ll"), (7, b"o")]
