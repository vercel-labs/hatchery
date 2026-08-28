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
            assert response.read() == b'{"ok": true, "version": 2}'
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

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps({"output": "done", "session_id": "ses_1"}).encode(), b""

        def send_signal(self, sent):
            pass

    async def create(*command, **options):
        commands.append((command, options))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

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
    assert commands[1][0][0:7] == ("fx", "ask", "--json", "--yolo", "--resume", "last", "--")
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
