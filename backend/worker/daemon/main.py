"""Small worker daemon: health, Queue polling, and fx process supervision."""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import fcntl
import hashlib
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
        self.processes: dict[str, TTYSession] = {}
        self.event_sequences = {key: int(value) for key, value in state.get("events", {}).items()}
        self.jobs: set[asyncio.Task] = set()
        self.streams: set[asyncio.Task] = set()
        self.input_locks: dict[int, threading.Lock] = {}
        self._input_generations: dict[int, int] = {}
        self._input_generation_lock = threading.Lock()
        self.fx_home = pathlib.Path(os.environ.get("FX_HOME", pathlib.Path.home() / ".fx"))
        self.instructions = os.environ.get("HATCHERY_AGENT_INSTRUCTIONS", "")

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
            session = self.processes.get(task_id)
            self.cancel_pending_input(session)
            if session is not None and session.exit_code is None:
                session.send_signal("interrupt")
            return
        if kind not in ("task.launch", "task.input"):
            return
        payload = raw.get("payload") or {}
        prompt = str(payload.get("prompt") or "")
        model = str(payload.get("model") or "")
        session = self.processes.get(task_id)
        if kind == "task.input" and session is not None and session.exit_code is None:
            job = asyncio.create_task(self._deliver(task_id, session, prompt))
        else:
            job = asyncio.create_task(
                self._launch(task_id, prompt, model, resume=kind == "task.input")
            )
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _launch(self, task_id: str, prompt: str, model: str, *, resume: bool) -> None:
        await self._emit(task_id, "task.started", {})
        try:
            self.configure_fx(model=model)
            self.prepare_workspace(self.workspace)
            env = os.environ.copy()
            env["FX_AUTO_UPGRADE"] = "0"
            session = TTYSession(task_id, self.fx_command(resume=resume), self.workspace, 80, 24, env)
            self.processes[task_id] = session
            with Handler.sessions_lock:
                Handler.sessions[task_id] = session
            stream = asyncio.create_task(self._stream_task(task_id, session))
            self.streams.add(stream)
            stream.add_done_callback(self.streams.discard)
            if prompt:
                await asyncio.to_thread(self.deliver_input, session, prompt, first=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._emit(task_id, "task.failed", {"error": str(error)})

    async def _deliver(self, task_id: str, session: "TTYSession", prompt: str) -> None:
        try:
            await asyncio.to_thread(self.deliver_input, session, prompt, first=False)
            await self._emit(task_id, "task.started", {})
        except asyncio.CancelledError:
            self.cancel_pending_input(session)
            raise
        except Exception as error:
            await self._emit(task_id, "task.failed", {"error": str(error)})

    async def _stream_task(self, task_id: str, session: "TTYSession") -> None:
        seen: set[str] = set()
        last_summary = ""
        stream = iter(
            self.stream_fx_events(
                self.workspace,
                seen=seen,
                stop=lambda: session.exit_code is not None,
            )
        )
        try:
            while session.exit_code is None:
                event = await asyncio.to_thread(next, stream, None)
                if event is None:
                    break
                if event["type"] == "assistant":
                    last_summary = event["text"]
                    await self._emit(task_id, "task.output", {"text": last_summary})
                elif event["type"] == "attention":
                    await self._emit(task_id, "task.question", {"question": event["text"]})
                elif event["type"] == "turn.completed":
                    await self._emit(task_id, "task.completed", {
                        "result": {
                            "summary": last_summary or "subagent completed",
                            "session_id": event.get("session_id"),
                            "tool_calls": [],
                        }
                    })
            exit_code = await asyncio.to_thread(session.wait)
            if exit_code != 0:
                _, output, _ = session.read(0, 0)
                await self._emit(task_id, "task.failed", {
                    "error": output.decode(errors="replace")[-4000:]
                })
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._emit(task_id, "task.failed", {"error": str(error)})
        finally:
            if self.processes.get(task_id) is session:
                self.processes.pop(task_id, None)

    def _write_private_json(self, path: pathlib.Path, updates: dict) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)

    def configure_fx(self, *, model: str = "", gateway_key: str = "", instructions: str = "") -> dict[str, str]:
        settings = {"permission_mode": "yolo", "yolo_acknowledged": True}
        if model:
            settings["model"] = model
        self._write_private_json(self.fx_home / "settings.json", settings)
        content = instructions or self.instructions
        if content:
            note = (
                "\n\n## Reaching MCP tools in fx\n\n"
                "MCP tools are reached through `mcp_search_tools` and `mcp_select_tool`. "
                "Search for the exact tool, select the returned tool, then call it. "
                "If search returns no tools, retry with the exact name rather than asking in prose.\n"
            )
            path = self.fx_home / "box-instructions.md"
            path.write_text(content + note, encoding="utf-8")
            path.chmod(0o600)
        key = gateway_key or os.environ.get("AI_GATEWAY_API_KEY", "")
        return {"AI_GATEWAY_API_KEY": key} if key else {}

    def configure_fx_mcp(
        self,
        name: str,
        *,
        url: str = "",
        command: list[str] | None = None,
        environment: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        path = self.fx_home / "mcp.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        servers = data.setdefault("mcp", {})
        if command:
            entry: dict = {"type": "local", "command": command, "enabled": True}
            if environment:
                entry["environment"] = environment
        else:
            entry = {"type": "http", "url": url, "enabled": not bool(headers)}
        servers[name] = entry
        self._write_private_json(path, data)

    def prepare_workspace(self, workspace: str) -> None:
        root = pathlib.Path(workspace)
        stored = self.fx_home / "box-instructions.md"
        if not stored.exists() or (root / ".git").exists():
            return
        content = stored.read_text(encoding="utf-8")
        if content.strip():
            (root / "AGENTS.md").write_text(content, encoding="utf-8")

    @staticmethod
    def fx_command(*, resume: bool = False) -> list[str]:
        command = ["fx"]
        if resume:
            command += ["--resume", "last"]
        return command

    def cancel_pending_input(self, session: "TTYSession" | None = None) -> None:
        with self._input_generation_lock:
            key = id(session) if session is not None else 0
            self._input_generations[key] = self._input_generations.get(key, 0) + 1

    def deliver_input(self, session: "TTYSession", text: str, *, first: bool = False) -> None:
        if not text:
            raise ValueError("fx input must not be empty")
        if session.exit_code is not None:
            raise LookupError("fx session is not running")
        key = id(session)
        with self._input_generation_lock:
            generation = self._input_generations.get(key, 0)
        lock = self.input_locks.setdefault(key, threading.Lock())
        with lock:
            if first:
                with session.condition:
                    while not session.output and session.exit_code is None:
                        session.condition.wait(0.05)
                        with self._input_generation_lock:
                            if generation != self._input_generations.get(key, 0):
                                return
            if session.exit_code is not None:
                raise LookupError("fx session is not running")
            with self._input_generation_lock:
                if generation != self._input_generations.get(key, 0):
                    return
            if not first:
                session.write(b"\x03")
            session.write(b"\x1b[200~" + text.encode() + b"\x1b[201~")
            session.write(b"\r")

    def discover_fx_session(self, workspace: str) -> str | None:
        canonical = str(pathlib.Path(workspace).resolve())
        latest = self.fx_home / "sessions" / "latest"
        hashed = latest / f"{hashlib.sha256(canonical.encode()).hexdigest()}.json"
        candidates = [hashed]
        try:
            candidates.extend(path for path in latest.glob("*.json") if path != hashed)
        except OSError:
            return None
        best: tuple[int, str] | None = None
        for path in candidates:
            try:
                pointer = json.loads(path.read_text(encoding="utf-8"))
                if str(pathlib.Path(pointer.get("workspace_root", "")).resolve()) != canonical:
                    continue
                candidate = (int(pointer.get("updated_at_ms", 0)), str(pointer["session_id"]))
                if best is None or candidate[0] > best[0]:
                    best = candidate
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return best[1] if best else None

    @staticmethod
    def decode_fx_event(record: dict, state: dict | None = None) -> list[dict]:
        state = state if state is not None else {}
        seen_calls = state.setdefault("calls", set())
        seen_results = state.setdefault("results", set())
        seen_turns = state.setdefault("turns", set())
        payload = record.get("payload") or {}
        kind = record.get("kind")
        source = str(record.get("event_id") or record.get("seq") or "")
        events: list[dict] = []
        execution = {}
        if kind == "recovery_checkpoint_set":
            checkpoint = payload.get("checkpoint") or {}
            turn_id = checkpoint.get("turn_id")
            user = (checkpoint.get("user") or {}).get("text")
            if user and turn_id not in seen_turns:
                seen_turns.add(turn_id)
                events.append({"type": "user", "text": user, "source_key": f"{source}:user"})
            execution = checkpoint.get("execution") or {}
        elif kind == "history_turn_committed":
            turn = payload.get("turn") or {}
            execution = turn.get("execution") or {}
        else:
            return events
        for step in execution.get("tool_steps") or []:
            for call in step.get("tool_calls") or []:
                call_id = str(call.get("id") or "")
                if not call_id or call_id in seen_calls:
                    continue
                seen_calls.add(call_id)
                raw = call.get("arguments_json") or "{}"
                try:
                    arguments = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    arguments = {"raw": raw}
                name = str(call.get("name") or "")
                normalized = {"name": name, "arguments": arguments}
                if name == "run_command":
                    normalized["command"] = arguments.get("command", "")
                elif name in ("read_file", "write_file", "edit_file"):
                    normalized["path"] = arguments.get("path", "")
                events.append({"type": "tool.call", "id": call_id, "tool": normalized, "source_key": f"{source}:call:{call_id}"})
            for result in step.get("tool_results") or []:
                call_id = str(result.get("tool_call_id") or "")
                if not call_id or call_id in seen_results:
                    continue
                seen_results.add(call_id)
                events.append({
                    "type": "tool.result",
                    "id": call_id,
                    "output": str(result.get("output") or ""),
                    "error": result.get("status") != "success",
                    "source_key": f"{source}:result:{call_id}",
                })
        if kind == "history_turn_committed":
            turn = payload.get("turn") or {}
            assistant = str(turn.get("assistant") or "")
            if assistant:
                events.append({"type": "assistant", "text": assistant, "source_key": f"{source}:assistant"})
            if turn.get("kind") == "interrupted" and turn.get("terminal_reason") == "cancelled":
                events.append({"type": "attention", "text": "the turn was cancelled", "source_key": f"{source}:cancelled"})
            else:
                events.append({"type": "turn.completed", "source_key": f"{source}:completed"})
        return events

    @classmethod
    def decode_fx_jsonl(cls, raw: bytes, state: dict | None = None) -> list[dict]:
        state = state if state is not None else {}
        events: list[dict] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            events.extend(cls.decode_fx_event(record, state))
        return events

    def stream_fx_events(
        self,
        workspace: str,
        *,
        seen: set[str] | None = None,
        wait: bool = True,
        stop=None,
    ):
        seen = seen if seen is not None else set()
        tailed: set[str] = set()
        session_id = self.discover_fx_session(workspace)
        while session_id is None:
            if not wait or (stop is not None and stop()):
                return
            time.sleep(0.15)
            session_id = self.discover_fx_session(workspace)
        while session_id:
            if stop is not None and stop():
                return
            if session_id in tailed:
                return
            tailed.add(session_id)
            path = self.fx_home / "sessions" / session_id / "events.jsonl"
            decoder_state: dict = {}
            initial_size = path.stat().st_size if path.exists() else 0
            follow_armed = True
            while True:
                if stop is not None and stop():
                    return
                if path.exists():
                    for event in self.decode_fx_jsonl(path.read_bytes(), decoder_state):
                        key = f"{session_id}:{event['source_key']}"
                        if key in seen:
                            continue
                        seen.add(key)
                        yield {**event, "source_key": key, "session_id": session_id}
                if not wait:
                    return
                time.sleep(0.15)
                current_size = path.stat().st_size if path.exists() else 0
                newer = self.discover_fx_session(workspace)
                if follow_armed and current_size == initial_size and newer and newer not in tailed:
                    session_id = newer
                    break
                if current_size != initial_size:
                    initial_size = current_size
                    follow_armed = False

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
