"""Small worker daemon: health, Queue polling, and fx process supervision."""

import argparse
import asyncio
import base64
import datetime
import fcntl
import http.server
import json
import os
import pathlib
import pty
import signal
import struct
import termios
import threading
import time
import uuid

VERSION = 3
REPLAY_LIMIT = 1024 * 1024


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class Runtime:
    """Execute ordered task commands with one resumable fx session per task."""

    def __init__(self, worker_id: str, workspace: str, publish, state_path: str | None = None):
        self.worker_id = worker_id
        self.workspace = workspace
        self.publish = publish
        self.state_path = pathlib.Path(state_path) if state_path else None
        state = {}
        if self.state_path and self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        self.sequences = {key: int(value) for key, value in state.get("commands", {}).items()}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.event_sequences = {key: int(value) for key, value in state.get("events", {}).items()}
        self.jobs: set[asyncio.Task] = set()

    async def handle(self, raw: dict) -> None:
        task_id = str(raw.get("task_id") or "")
        if not task_id or raw.get("worker_id") != self.worker_id:
            return
        sequence = int(raw.get("sequence", -1))
        if sequence <= self.sequences.get(task_id, -1):
            return
        self.sequences[task_id] = sequence
        self._save_state()
        kind = raw.get("type")
        if kind == "task.cancel":
            with Handler.sessions_lock:
                session = Handler.sessions.get(task_id)
            if session is not None and session.exit_code is None:
                session.send_signal("interrupt")
            return
        if kind not in ("task.launch", "task.input"):
            return
        job = asyncio.create_task(
            self._run(
                task_id,
                str((raw.get("payload") or {}).get("prompt") or ""),
                str((raw.get("payload") or {}).get("model") or ""),
                resume=kind == "task.input",
            )
        )
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _run(self, task_id: str, prompt: str, model: str, *, resume: bool) -> None:
        await self._emit(task_id, "task.started", {})
        command = ["fx", "ask", "--json", "--yolo"]
        if resume:
            command += ["--resume", "last"]
        command += ["--", prompt]
        env = os.environ.copy()
        env["FX_PERMISSION_MODE"] = "yolo"
        env["FX_AUTO_UPGRADE"] = "0"
        if model:
            env["FX_MODEL"] = model
        try:
            session = TTYSession(task_id, command, self.workspace, 80, 24, env)
            with Handler.sessions_lock:
                Handler.sessions[task_id] = session
            exit_code = await asyncio.to_thread(session.wait)
            _, stdout, _ = session.read(0, 0)
            if exit_code == 0:
                text = stdout.decode(errors="replace").strip()
                try:
                    data = json.loads(text or "{}")
                except json.JSONDecodeError:
                    data = {"output": text}
                output = str(data.get("output") or "subagent completed")
                await self._emit(task_id, "task.output", {"text": output})
                await self._emit(
                    task_id,
                    "task.completed",
                    {
                        "result": {
                            "summary": output,
                            "session_id": data.get("session_id"),
                            "tool_calls": data.get("tool_calls", []),
                        }
                    },
                )
            else:
                error = stdout.decode(errors="replace").strip()
                await self._emit(task_id, "task.failed", {"error": error[-4000:]})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._emit(task_id, "task.failed", {"error": str(error)})
        finally:
            self.processes.pop(task_id, None)

    async def _emit(self, task_id: str, kind: str, payload: dict) -> None:
        sequence = self.event_sequences.get(task_id, -1) + 1
        self.event_sequences[task_id] = sequence
        self._save_state()
        await self.publish(
            {
                "version": 1,
                "id": f"evt_{uuid.uuid4().hex}",
                "worker_id": self.worker_id,
                "task_id": task_id,
                "sequence": sequence,
                "type": kind,
                "created_at": _now(),
                "payload": payload,
            }
        )

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"commands": self.sequences, "events": self.event_sequences}),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)


class TTYSession:
    @staticmethod
    def new_id() -> str:
        return f"tty_{uuid.uuid4().hex}"

    def __init__(
        self,
        session_id: str,
        command: list[str],
        workspace: str,
        cols: int,
        rows: int,
        env: dict[str, str] | None = None,
    ):
        self.id = session_id
        self.command = command
        self.workspace = workspace
        self.output = bytearray()
        self.base_offset = 0
        self.exit_code: int | None = None
        self.running = True
        self.last_output_at: str | None = None
        self.cols = cols
        self.rows = rows
        self.condition = threading.Condition()
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(workspace)
            os.execvpe(command[0], command, env or os.environ.copy())
        self.pid = pid
        self.fd = fd
        self.resize(cols, rows)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.condition:
                self.output.extend(data)
                self.last_output_at = _now()
                if len(self.output) > REPLAY_LIMIT:
                    drop = len(self.output) - REPLAY_LIMIT
                    del self.output[:drop]
                    self.base_offset += drop
                self.condition.notify_all()
        _, status = os.waitpid(self.pid, 0)
        with self.condition:
            self.exit_code = os.waitstatus_to_exitcode(status)
            self.running = False
            self.condition.notify_all()
        try:
            os.close(self.fd)
        except OSError:
            pass

    def read(self, offset: int, timeout: float = 25) -> tuple[int, bytes, int | None]:
        deadline = time.monotonic() + timeout
        with self.condition:
            if offset < self.base_offset:
                raise ValueError(
                    f"offset {offset} is outside replay window starting at {self.base_offset}"
                )
            while offset >= self.base_offset + len(self.output) and self.exit_code is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
                if offset < self.base_offset:
                    raise ValueError(
                        f"offset {offset} is outside replay window starting at {self.base_offset}"
                    )
            start = offset - self.base_offset
            return offset, bytes(self.output[start:]), self.exit_code

    def wait(self) -> int:
        with self.condition:
            while self.exit_code is None:
                self.condition.wait()
            return self.exit_code

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def resize(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        with self.condition:
            self.cols = cols
            self.rows = rows

    def geometry(self) -> tuple[int, int]:
        with self.condition:
            return self.cols, self.rows

    def snapshot(self) -> dict:
        with self.condition:
            return {
                "id": self.id,
                "running": self.running,
                "exit_code": self.exit_code,
                "cols": self.cols,
                "rows": self.rows,
                "last_output_at": self.last_output_at,
            }

    def send_signal(self, name: str) -> None:
        sent = {"interrupt": signal.SIGINT, "terminate": signal.SIGTERM, "kill": signal.SIGKILL}[name]
        try:
            os.killpg(self.pid, sent)
        except ProcessLookupError:
            pass


class Handler(http.server.BaseHTTPRequestHandler):
    sessions: dict[str, TTYSession] = {}
    sessions_lock = threading.Lock()
    workspace = "/vercel/sandbox"

    @classmethod
    def list_sessions(cls) -> list[dict]:
        with cls.sessions_lock:
            sessions = list(cls.sessions.values())
        return [session.snapshot() for session in sessions]

    def _authorized(self) -> bool:
        token = os.environ.get("HATCHERY_DAEMON_TOKEN")
        if token and self.headers.get("authorization") == f"Bearer {token}":
            return True
        self.send_error(401)
        return False

    def _json(self) -> dict:
        size = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path == "/health":
            self._send_json({"ok": True, "version": VERSION})
            return
        if self.path == "/tty":
            self._send_json({"sessions": self.list_sessions()})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "tty":
            self.send_error(404)
            return
        session_id, action = parts[1:]
        body = self._json()
        with self.sessions_lock:
            session = self.sessions.get(session_id)
            if session is None and action == "read" and body.get("command"):
                session = TTYSession(
                    session_id,
                    [str(item) for item in body["command"]],
                    self.workspace,
                    int(body.get("cols", 80)),
                    int(body.get("rows", 24)),
                )
                self.sessions[session_id] = session
        if session is None:
            self.send_error(404)
            return
        if action == "read":
            offset, data, exit_code = session.read(int(body.get("offset", 0)))
            self._send_json({
                "offset": offset,
                "data": base64.b64encode(data).decode(),
                "exit_code": exit_code,
            })
        elif action == "input":
            session.write(base64.b64decode(body.get("data", "")))
            self._send_json({"ok": True})
        elif action == "resize":
            session.resize(int(body.get("cols", 80)), int(body.get("rows", 24)))
            self._send_json({"ok": True})
        elif action == "signal" and body.get("signal") in ("interrupt", "terminate", "kill"):
            session.send_signal(body["signal"])
            self._send_json({"ok": True})
        else:
            self.send_error(400)

    def log_message(self, format: str, *args: object) -> None:
        pass


def source() -> str:
    """Return this standalone module for installation in a sandbox."""
    return pathlib.Path(__file__).read_text(encoding="utf-8")


async def run(worker_id: str, workspace: str, port: int, state_path: str) -> None:
    from vercel import queue

    async def publish(event: dict) -> None:
        await queue.send("hatchery-worker-events-v1", event, idempotency_key=event["id"])

    runtime = Runtime(worker_id, workspace, publish, state_path)
    Handler.workspace = workspace
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while True:
            delivered = False
            async for delivery in queue.poll(
                f"hatchery-worker-{worker_id}-commands-v1",
                f"hatchery-daemon-{worker_id}",
                limit=10,
                lease_duration=300,
            ):
                delivered = True
                async with delivery as message:
                    await runtime.handle(message.payload)
            if not delivered:
                await asyncio.sleep(1)
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--worker-id", default=os.environ.get("HATCHERY_WORKER_ID"))
    parser.add_argument("--workspace", default=os.environ.get("HATCHERY_WORKSPACE", "/vercel/sandbox"))
    parser.add_argument("--state", default="/opt/hatchery/daemon-state.json")
    args = parser.parse_args()
    if not args.worker_id:
        # Health-only mode remains useful for installation checks.
        http.server.ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
        return
    asyncio.run(run(args.worker_id, args.workspace, args.port, args.state))


if __name__ == "__main__":
    main()
