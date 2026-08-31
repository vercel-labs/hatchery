"""Small worker daemon: health, Queue polling, and fx process supervision."""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import datetime
import fcntl
import hashlib
import http
import http.server
import ipaddress
import json
import os
import pathlib
import pty
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
import uuid

import asyncssh
import websockets.asyncio.server

VERSION = 10
REPLAY_LIMIT = 1024 * 1024
FX_INPUT_READY = b"\x1b[?2004h"
FX_INTERRUPT_SETTLE = 0.75
FX_SUBMIT_BEAT = 1.0
SSH_PORT = 8788
SSH_INTERNAL_PORT = 8022
SSH_STREAM_GRACE = 300


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def agent_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the agent environment without daemon/control-plane credentials."""
    source = dict(env or os.environ)
    private = {
        "HATCHERY_DAEMON_TOKEN",
        "HATCHERY_WORKER_ID",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_QUEUE_TOKEN",
        "VERCEL_QUEUE_BASE_URL",
        "VERCEL_DEPLOYMENT_ID",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
    result = {name: value for name, value in source.items() if name not in private}
    result["PATH"] = f"/opt/hatchery/bin:{result.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    return result


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
        self.active = {
            str(key): {"model": str(value.get("model") or "")}
            for key, value in state.get("active", {}).items()
            if isinstance(value, dict)
        }
        self.jobs: set[asyncio.Task] = set()
        self.streams: set[asyncio.Task] = set()
        self.command_lock = asyncio.Lock()
        self.input_locks: dict[int, threading.Lock] = {}
        self._input_generations: dict[int, int] = {}
        self._input_generation_lock = threading.Lock()
        self.fx_home = pathlib.Path(os.environ.get("FX_HOME", pathlib.Path.home() / ".fx"))
        self.instructions = os.environ.get("HATCHERY_AGENT_INSTRUCTIONS", "")
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sign_commits = None

    async def handle(self, raw: dict) -> None:
        """Accept one command only after its process handoff is complete.

        Queue delivery acknowledgement follows this method returning. Persisting the
        sequence before scheduling background work could therefore lose a launch if
        the daemon crashed between those two steps.
        """
        task_id = str(raw.get("task_id") or "")
        if not task_id or raw.get("worker_id") != self.worker_id:
            return
        sequence = int(raw.get("sequence", -1))
        async with self.command_lock:
            if sequence <= self.sequences.get(task_id, -1):
                return
            kind = raw.get("type")
            if kind == "task.cancel":
                session = self.processes.get(task_id)
                self.cancel_pending_input(session)
                if session is not None and session.exit_code is None:
                    session.send_signal("interrupt")
            elif kind in ("task.launch", "task.input"):
                payload = raw.get("payload") or {}
                prompt = str(payload.get("prompt") or "")
                model = str(payload.get("model") or "")
                session = self.processes.get(task_id)
                if kind == "task.input" and session is not None and session.exit_code is None:
                    await self._deliver(task_id, session, prompt)
                else:
                    await self._launch(task_id, prompt, model, resume=kind == "task.input")
            self.sequences[task_id] = sequence
            self._save_state()

    async def _launch(self, task_id: str, prompt: str, model: str, *, resume: bool) -> None:
        try:
            self.configure_fx(model=model)
            self.prepare_workspace(self.workspace)
            env = agent_environment()
            env["FX_AUTO_UPGRADE"] = "0"
            env["HATCHERY_ACTIVE_TASK"] = task_id
            env["HATCHERY_WORKSPACE"] = self.workspace
            session = TTYSession(task_id, self.fx_command(resume=resume), self.workspace, 80, 24, env)
            self.processes[task_id] = session
            self.active[task_id] = {"model": model}
            self._save_state()
            with Handler.sessions_lock:
                Handler.sessions[task_id] = session
            await self._emit(task_id, "task.started", {})
            stream = asyncio.create_task(self._stream_task(task_id, session))
            self.streams.add(stream)
            stream.add_done_callback(self.streams.discard)
            if prompt:
                await asyncio.to_thread(self.deliver_input, session, prompt, first=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._emit(task_id, "task.failed", {"error": str(error)})
            raise

    async def _deliver(self, task_id: str, session: "TTYSession", prompt: str) -> None:
        try:
            await asyncio.to_thread(self.deliver_input, session, prompt, first=False)
            await self._emit(task_id, "task.started", {})
        except asyncio.CancelledError:
            self.cancel_pending_input(session)
            raise
        except Exception as error:
            await self._emit(task_id, "task.failed", {"error": str(error)})
            raise

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
                    await self._emit(task_id, "task.output", {
                        "text": last_summary,
                        "session_id": event.get("session_id"),
                    })
                elif event["type"] in ("user", "tool.call", "tool.result"):
                    await self._emit(task_id, "task.transcript", self.transcript_payload(event))
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
                    self.active.pop(task_id, None)
                    self._save_state()
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
                    while FX_INPUT_READY not in session.output and session.exit_code is None:
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
                time.sleep(FX_INTERRUPT_SETTLE)
            session.write(b"\x1b[200~" + text.encode() + b"\x1b[201~")
            time.sleep(FX_SUBMIT_BEAT)
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
                updated_at_ms = pointer.get("updated_at_ms")
                session_id = pointer.get("session_id")
                if not isinstance(updated_at_ms, int) or not isinstance(session_id, str):
                    continue
                candidate = (updated_at_ms, session_id)
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

    def recover(self) -> dict:
        """Return the persisted transport cursors loaded during construction."""
        return {
            "commands": dict(self.sequences),
            "events": dict(self.event_sequences),
            "active": dict(self.active),
        }

    async def recover_active_tasks(self) -> None:
        """Reconnect tasks whose fx process was owned by a previous daemon."""
        for task_id, state in list(self.active.items()):
            await self._launch(task_id, "", state.get("model", ""), resume=True)

    @staticmethod
    def meaningful_input(text: str) -> bool:
        return bool(text and text.strip())

    @staticmethod
    def is_runtime_echo(text: str, prompt: str = "") -> bool:
        cleaned = text.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
        return bool(prompt) and cleaned == prompt.strip()

    @staticmethod
    def task_context(repos: list[str] | None = None, *, existing_prs: int = 0) -> str:
        repos = repos or []
        if repos:
            listed = "\n".join(f"- {repo}" for repo in repos)
            context = f"Repositories:\n{listed}\n"
            if existing_prs == 0:
                context += "Create a pull request when the task requires code changes.\n"
            elif existing_prs == 1:
                context += "Update the existing pull request instead of creating another.\n"
            else:
                context += "Inspect the existing pull requests and update the relevant one.\n"
            return context
        return "No repository is attached; work in the provided workspace.\n"

    @classmethod
    def task_prompt(
        cls,
        task: str,
        inputs: list[str] | None = None,
        *,
        repos: list[str] | None = None,
        existing_prs: int = 0,
        first: bool = True,
    ) -> str:
        protocol = (
            "If you need information from the user, use ask_user_question; prose alone does not reach them. "
            "When finished, stop; there is no completion tool."
        )
        pending = "\n\n".join(value.strip() for value in (inputs or []) if value.strip())
        if not first:
            return f"{pending}\n\n{protocol}".strip()
        body = f"Task:\n{task.strip()}\n\n{cls.task_context(repos, existing_prs=existing_prs)}"
        if pending:
            body += f"\nAdditional input:\n{pending}\n"
        return f"{body}\n{protocol}".strip()

    def build_workspace(self, task_id: str | None = None) -> str:
        path = pathlib.Path(self.workspace)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    async def launch_recorded_task(self, task_id: str, prompt: str, model: str = "") -> None:
        if not self.meaningful_input(prompt):
            await self._emit(task_id, "task.failed", {"error": "task has no initial prompt"})
            return
        await self._launch(task_id, prompt, model, resume=False)

    async def deliver_pending_input(
        self,
        task_id: str,
        inputs: list[str] | str,
        model: str = "",
        *,
        max_bytes: int = 32 * 1024,
    ) -> int:
        values = [inputs] if isinstance(inputs, str) else inputs
        batch: list[str] = []
        size = 0
        for value in values:
            if not self.meaningful_input(value):
                continue
            encoded = value.encode()
            if batch and size + len(encoded) > max_bytes:
                break
            batch.append(value)
            size += len(encoded)
            if size >= max_bytes:
                break
        if not batch:
            return 0
        session = self.processes.get(task_id)
        prompt = "\n\n".join(batch)
        if session is not None and session.exit_code is None:
            await self._deliver(task_id, session, prompt)
        else:
            await self._launch(task_id, prompt, model, resume=True)
        return len(batch)

    @staticmethod
    def transcript_payload(event: dict, max_text: int = 8 * 1024) -> dict:
        payload = {"kind": str(event.get("type") or "event")}
        if event.get("session_id"):
            payload["session_id"] = str(event["session_id"])
        if event.get("type") in ("user", "assistant"):
            text = str(event.get("text") or "")
            payload.update(text=text[:max_text], truncated=len(text) > max_text)
        elif event.get("type") == "tool.call":
            tool = event.get("tool") or {}
            arguments = json.dumps(tool.get("arguments") or {}, ensure_ascii=False)
            payload.update(
                tool_call_id=str(event.get("id") or ""),
                tool_name=str(tool.get("name") or ""),
                arguments=arguments[:max_text],
                truncated=len(arguments) > max_text,
            )
        elif event.get("type") == "tool.result":
            output = str(event.get("output") or "")
            payload.update(
                tool_call_id=str(event.get("id") or ""),
                output=output[:max_text],
                error=bool(event.get("error")),
                truncated=len(output) > max_text,
            )
        return payload

    async def ingest_fx_event(self, task_id: str, event: dict) -> None:
        """Publish one decoded fx event through the daemon's ordered event channel."""
        kind = event.get("type")
        if kind == "assistant":
            await self._emit(task_id, "task.output", {
                "text": event.get("text", ""),
                "session_id": event.get("session_id"),
            })
        elif kind in ("user", "tool.call", "tool.result"):
            await self._emit(task_id, "task.transcript", self.transcript_payload(event))
        elif kind == "attention":
            await self._emit(task_id, "task.question", {"question": event.get("text", "input required")})
        elif kind == "turn.completed":
            await self._emit(task_id, "task.completed", {
                "result": {
                    "summary": event.get("summary") or "subagent completed",
                    "session_id": event.get("session_id"),
                    "tool_calls": [],
                }
            })

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
            json.dumps({
                "commands": self.sequences,
                "events": self.event_sequences,
                "active": self.active,
            }),
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
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError("PTY write made no progress")
            view = view[written:]

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


def allow_ssh_forward(host: str) -> bool:
    """Allow SSH forwarding only to services bound inside this sandbox."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        return allow_ssh_forward(dest_host)

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return False


async def run_ssh_process(process: asyncssh.SSHServerProcess, workspace: str) -> None:
    """Run an SSH shell or exec with normal PTY and pipe semantics."""
    command = process.command or "/bin/bash -l"
    env = agent_environment()
    env.update({str(name): str(value) for name, value in process.env.items()})
    if process.term_type:
        env["TERM"] = process.term_type
        child = TTYSession(
            f"ssh_{uuid.uuid4().hex}",
            ["/bin/sh", "-lc", command],
            workspace,
            process.term_size[0] or 80,
            process.term_size[1] or 24,
            env,
        )

        async def input_to_pty() -> None:
            while child.exit_code is None:
                try:
                    data = await process.stdin.read(65536)
                except asyncssh.TerminalSizeChanged as changed:
                    child.resize(changed.width, changed.height)
                    continue
                if not data:
                    return
                child.write(data)

        async def pty_to_output() -> None:
            offset = 0
            while True:
                start, data, exit_code = await asyncio.to_thread(child.read, offset, 1)
                if data:
                    process.stdout.write(data)
                    offset = start + len(data)
                if exit_code is not None:
                    process.exit(exit_code)
                    return

        tasks = [asyncio.create_task(input_to_pty()), asyncio.create_task(pty_to_output())]
        try:
            await tasks[1]
        finally:
            tasks[0].cancel()
        return

    child = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-lc",
        command,
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def copy_input() -> None:
        assert child.stdin is not None
        while data := await process.stdin.read(65536):
            child.stdin.write(data)
            await child.stdin.drain()
        child.stdin.close()

    async def copy_output(reader: asyncio.StreamReader, writer) -> None:
        while data := await reader.read(65536):
            writer.write(data)

    assert child.stdout is not None and child.stderr is not None
    input_task = asyncio.create_task(copy_input())
    try:
        await asyncio.gather(
            copy_output(child.stdout, process.stdout),
            copy_output(child.stderr, process.stderr),
        )
        process.exit(await child.wait())
    finally:
        input_task.cancel()


class SSHStream:
    """A TCP connection to the SSH server which survives WebSocket replacement."""

    def __init__(self, stream_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.id = stream_id
        self.reader = reader
        self.writer = writer
        self.output = bytearray()
        self.base_offset = 0
        self.input_offset = 0
        self.condition = asyncio.Condition()
        self.closed = False
        self.last_attached = time.monotonic()
        self.reader_task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while data := await self.reader.read(65536):
                async with self.condition:
                    self.output.extend(data)
                    if len(self.output) > REPLAY_LIMIT:
                        drop = len(self.output) - REPLAY_LIMIT
                        del self.output[:drop]
                        self.base_offset += drop
                    self.condition.notify_all()
        finally:
            async with self.condition:
                self.closed = True
                self.condition.notify_all()

    async def write(self, offset: int, data: bytes) -> None:
        if offset > self.input_offset:
            raise ValueError("SSH input has a gap")
        overlap = self.input_offset - offset
        if overlap < len(data):
            self.writer.write(data[overlap:])
            await self.writer.drain()
            self.input_offset += len(data) - overlap

    async def read(self, offset: int) -> tuple[int, bytes, bool]:
        async with self.condition:
            if offset < self.base_offset:
                offset = self.base_offset
            while offset >= self.base_offset + len(self.output) and not self.closed:
                await self.condition.wait()
            start = offset - self.base_offset
            return offset, bytes(self.output[start:]), self.closed

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


class SSHService:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.streams: dict[str, SSHStream] = {}
        self.ssh_server = None
        self.websocket_server = None
        self.ssh_port = SSH_INTERNAL_PORT
        self.websocket_port = SSH_PORT

    async def start(self, websocket_port: int = SSH_PORT, ssh_port: int = SSH_INTERNAL_PORT) -> None:
        self.websocket_port = websocket_port
        self.ssh_port = ssh_port
        key_path = pathlib.Path(os.environ.get("HATCHERY_SSH_HOST_KEY", "/opt/hatchery/ssh_host_key"))
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            host_key = asyncssh.read_private_key(key_path)
        else:
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            key_path.write_bytes(host_key.export_private_key())
            key_path.chmod(0o600)
        self.ssh_server = await asyncssh.create_server(
            SSHServer,
            "127.0.0.1",
            ssh_port,
            server_host_keys=[host_key],
            process_factory=lambda process: run_ssh_process(process, self.workspace),
            encoding=None,
            line_editor=False,
        )
        self.ssh_port = self.ssh_server.get_port()
        self.websocket_server = await websockets.asyncio.server.serve(
            self.handle,
            "0.0.0.0",
            websocket_port,
            process_request=self.authorize,
            max_size=None,
            compression=None,
        )
        self.websocket_port = self.websocket_server.sockets[0].getsockname()[1]

    async def authorize(self, connection, request):
        token = os.environ.get("HATCHERY_DAEMON_TOKEN", "")
        if token and request.headers.get("authorization") == f"Bearer {token}":
            return None
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")

    async def handle(self, websocket) -> None:
        parsed = urllib.parse.urlsplit(websocket.request.path)
        if parsed.path == "/tty":
            await self.handle_tty(websocket, urllib.parse.parse_qs(parsed.query))
            return
        query = urllib.parse.parse_qs(parsed.query)
        requested = query.get("stream_id", ["new"])[0]
        output_offset = int(query.get("offset", ["0"])[0])
        if requested == "new":
            reader, writer = await asyncio.open_connection("127.0.0.1", self.ssh_port)
            stream = SSHStream(f"ssh_{uuid.uuid4().hex}", reader, writer)
            self.streams[stream.id] = stream
        else:
            stream = self.streams.get(requested)
            if stream is None:
                await websocket.close(4404, "stream not found")
                return
        stream.last_attached = time.monotonic()
        await websocket.send(json.dumps({
            "stream_id": stream.id,
            "offset": max(output_offset, stream.base_offset),
            "input_offset": stream.input_offset,
        }))

        async def upload() -> None:
            async for message in websocket:
                if not isinstance(message, bytes) or len(message) < 8:
                    continue
                await stream.write(int.from_bytes(message[:8], "big"), message[8:])

        async def download() -> None:
            offset = output_offset
            while True:
                start, data, closed = await stream.read(offset)
                if data:
                    await websocket.send(start.to_bytes(8, "big") + data)
                    offset = start + len(data)
                if closed:
                    await websocket.close()
                    return

        tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled():
                task.result()
        if stream.closed:
            self.streams.pop(stream.id, None)

    async def handle_tty(self, websocket, query: dict[str, list[str]]) -> None:
        """Attach one WebSocket directly to a durable PTY session."""
        try:
            raw = await websocket.recv()
            if not isinstance(raw, str):
                raise ValueError("TTY attach frame must be JSON text")
            attach = json.loads(raw)
            session_id = str(attach["session_id"])
            offset = max(0, int(attach.get("offset", 0)))
            cols = max(1, int(attach.get("cols", 80)))
            rows = max(1, int(attach.get("rows", 24)))
            command = attach.get("command")
            if command is not None:
                command = [str(item) for item in command]
                if not command:
                    raise ValueError("TTY command must not be empty")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            await websocket.close(4400, str(error))
            return

        with Handler.sessions_lock:
            session = Handler.sessions.get(session_id)
            if session is None and command is not None:
                session = TTYSession(
                    session_id,
                    command,
                    self.workspace,
                    cols,
                    rows,
                    agent_environment(),
                )
                Handler.sessions[session_id] = session
        if session is None:
            await websocket.close(4404, "session not found")
            return

        session.resize(cols, rows)
        with session.condition:
            offset = max(offset, session.base_offset)
        await websocket.send(json.dumps({
            "type": "handshake",
            "body": {
                "sessionId": session_id,
                "offset": offset,
                "cols": cols,
                "rows": rows,
            },
        }))

        async def upload() -> None:
            async for message in websocket:
                if not isinstance(message, str):
                    continue
                try:
                    frame = json.loads(message)
                    body = frame.get("body") or {}
                    if frame.get("type") == "tty-input":
                        session.write(base64.b64decode(body.get("data", ""), validate=True))
                    elif frame.get("type") == "resize":
                        session.resize(max(1, int(body["cols"])), max(1, int(body["rows"])))
                    elif frame.get("type") == "signal" and body.get("signal") in (
                        "interrupt", "terminate", "kill"
                    ):
                        session.send_signal(body["signal"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue

        async def download() -> None:
            current = offset
            while True:
                start, data, exit_code = await asyncio.to_thread(session.read, current)
                if data:
                    await websocket.send(json.dumps({
                        "type": "tty-output",
                        "body": {"data": base64.b64encode(data).decode()},
                    }))
                    current = start + len(data)
                if exit_code is not None:
                    await websocket.send(json.dumps({
                        "type": "exit", "body": {"code": exit_code}
                    }))
                    return

        tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled():
                task.result()

    async def stop(self) -> None:
        for stream in list(self.streams.values()):
            await stream.close()
        if self.websocket_server is not None:
            self.websocket_server.close()
            await self.websocket_server.wait_closed()
        if self.ssh_server is not None:
            self.ssh_server.close()
            await self.ssh_server.wait_closed()


class Handler(http.server.BaseHTTPRequestHandler):
    sessions: dict[str, TTYSession] = {}
    sessions_lock = threading.Lock()
    workspace = "/vercel"
    runtime: Runtime | None = None
    queue_connected = False
    queue_error: str | None = None

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
            self._send_json({
                "ok": True,
                "version": VERSION,
                "queue_connected": self.queue_connected,
                "queue_error": self.queue_error,
                "event_deployment": os.environ.get("HATCHERY_EVENT_DEPLOYMENT"),
            })
            return
        if self.path == "/tty":
            self._send_json({"sessions": self.list_sessions()})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        loopback = self.client_address[0] in ("127.0.0.1", "::1")
        if self.path in ("/pr-created", "/sign-commits"):
            if not loopback:
                self.send_error(403)
                return
            body = self._json()
            if self.path == "/pr-created":
                url = str(body.get("url") or "")
                if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", url):
                    self.send_error(400)
                    return
                runtime = self.runtime
                task_id = str(body.get("task_id") or "")
                if body.get("workspace") != self.workspace:
                    self.send_error(403)
                    return
                if runtime is not None and task_id:
                    asyncio.run_coroutine_threadsafe(
                        runtime._emit(task_id, "task.output", {"pull_request": body}),
                        runtime.loop,
                    )
                self._send_json({"ok": True})
                return
            if self.runtime is None or self.runtime.sign_commits is None:
                self.send_error(503, "commit signing is not configured")
                return
            repo = body.get("repo") or {}
            owner = str(repo.get("owner") or "")
            name = str(repo.get("name") or "")
            requested = [str(item.get("sha") or "") for item in body.get("commits") or []]
            try:
                origin = subprocess.run(
                    ["git", "-C", self.workspace, "remote", "get-url", "origin"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                match = re.search(r"github\.com[/:]([^/]+)/([^/#]+?)(?:\.git)?$", origin)
                if match is None or (owner, name) != match.groups() or not requested:
                    raise ValueError("sign request does not match the workspace repository")
                for sha in requested:
                    subprocess.run(
                        ["git", "-C", self.workspace, "cat-file", "-e", f"{sha}^{{commit}}"],
                        capture_output=True, check=True,
                    )
                signed = self.runtime.sign_commits(body)
            except Exception as error:
                self.send_error(502, str(error))
                return
            self._send_json({"signed_shas": signed})
            return
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
                    agent_environment(),
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


async def _sign_commits(connect, connector: str, request: dict) -> list[str]:
    import httpx

    token = await connect.get_token(connector, subject=connect.ConnectAppTokenSubject())
    repo = request.get("repo") or {}
    owner = str(repo.get("owner") or "")
    name = str(repo.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid GitHub repository")
    signed: list[str] = []
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=60) as client:
        for commit in request.get("commits") or []:
            parents = [signed[-1]] if signed else [str(item) for item in commit.get("parents") or []]
            body = {
                "message": str(commit.get("message") or ""),
                "tree": str(commit.get("tree_sha") or ""),
                "parents": parents,
            }
            author = commit.get("original_author")
            if isinstance(author, dict) and author.get("name") and author.get("email"):
                trailer = f"Co-Authored-By: {author['name']} <{author['email']}>"
                if trailer not in body["message"]:
                    body["message"] = body["message"].rstrip() + f"\n\n{trailer}\n"
            response = await client.post(f"/repos/{owner}/{name}/git/commits", json=body)
            response.raise_for_status()
            signed.append(str(response.json()["sha"]))
    return signed


async def poll_commands(queue, runtime: Runtime, worker_id: str, sleep=asyncio.sleep) -> None:
    while True:
        delivered = False
        try:
            async for delivery in queue.poll(
                f"hatchery-worker-{worker_id}-commands-v1",
                f"hatchery-daemon-{worker_id}",
                limit=10,
                lease_duration=300,
            ):
                Handler.queue_connected = True
                Handler.queue_error = None
                delivered = True
                await redeliver_command(delivery, runtime)
            Handler.queue_connected = True
            Handler.queue_error = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            Handler.queue_connected = False
            Handler.queue_error = f"{type(error).__name__}: {error}"[:500]
            print(f"queue poll failed: {Handler.queue_error}", file=sys.stderr, flush=True)
            await sleep(2)
            continue
        if not delivered:
            await sleep(1)


async def redeliver_command(delivery, runtime: Runtime) -> None:
    """Acknowledge a Queue command only after durable runtime acceptance."""
    async with delivery as message:
        await runtime.handle(message.payload)


async def run(worker_id: str, workspace: str, port: int, state_path: str) -> None:
    from vercel import connect, queue

    queue_client = queue.QueueClient(
        token=os.environ.get("VERCEL_QUEUE_TOKEN"),
        region=os.environ.get("VERCEL_REGION"),
        deployment=queue.ALL_DEPLOYMENTS,
    )
    event_deployment = os.environ.get("HATCHERY_EVENT_DEPLOYMENT")

    async def publish(event: dict) -> None:
        await queue_client.send(
            "hatchery-worker-events-v1",
            event,
            idempotency_key=event["id"],
            deployment=event_deployment or queue.ALL_DEPLOYMENTS,
        )

    runtime = Runtime(worker_id, workspace, publish, state_path)
    connector = os.environ.get("GITHUB_CONNECTOR", "")
    if connector:
        def sign_commits(request: dict) -> list[str]:
            future = asyncio.run_coroutine_threadsafe(
                _sign_commits(connect, connector, request), runtime.loop
            )
            return future.result(timeout=300)
        runtime.sign_commits = sign_commits
    runtime.loop = asyncio.get_running_loop()
    await runtime.recover_active_tasks()
    Handler.workspace = workspace
    Handler.runtime = runtime
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ssh = SSHService(workspace)
    await ssh.start()
    try:
        await poll_commands(queue_client, runtime, worker_id)
    finally:
        await ssh.stop()
        server.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--worker-id", default=os.environ.get("HATCHERY_WORKER_ID"))
    parser.add_argument("--workspace", default=os.environ.get("HATCHERY_WORKSPACE", "/vercel"))
    parser.add_argument("--state", default="/opt/hatchery/daemon-state.json")
    args = parser.parse_args()
    if not args.worker_id:
        # Health-only mode remains useful for installation checks.
        http.server.ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
        return
    asyncio.run(run(args.worker_id, args.workspace, args.port, args.state))


if __name__ == "__main__":
    main()
