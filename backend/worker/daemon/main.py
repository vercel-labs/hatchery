"""Small worker daemon: health, Queue polling, and fx process supervision."""

import argparse
import asyncio
import datetime
import http.server
import json
import os
import pathlib
import signal
import threading
import uuid

VERSION = 2


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
            process = self.processes.get(task_id)
            if process and process.returncode is None:
                process.send_signal(signal.SIGINT)
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
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.workspace,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.processes[task_id] = process
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                data = json.loads(stdout.decode() or "{}")
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
                error = stderr.decode(errors="replace").strip() or stdout.decode(
                    errors="replace"
                ).strip()
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


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        token = os.environ.get("HATCHERY_DAEMON_TOKEN")
        if not token or self.headers.get("authorization") != f"Bearer {token}":
            self.send_error(401)
            return
        body = json.dumps({"ok": True, "version": VERSION}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
