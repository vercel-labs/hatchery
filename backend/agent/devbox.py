"""Thin client for the devbox platform: the worker layer.

A devbox runs a real coding agent (claude code) in a durable pty session per
task. Everything inbound to us is push: task events arrive over the box's
watch websocket (local dev), or per-task webhooks (deployed). The pty is
attachable over the box's /__tty websocket — that's the UI's terminal pane.

Auth is a Vercel user bearer. Locally we fall back to the vercel CLI's
auth.json so `vc login` is all the setup there is.
"""

import asyncio
import json
import os
import pathlib

import httpx
import websockets

API = os.environ.get("DEVBOX_API_URL", "https://api.vercel.com")
_CLI = pathlib.Path.home() / "Library/Application Support/com.vercel.cli"

TERMINAL_STATES = ("complete", "errored")


def _checked(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:  # keep the body; api-devbox errors are useful
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    return r


def token() -> str:
    if t := os.environ.get("VERCEL_TOKEN"):
        return t
    return json.loads((_CLI / "auth.json").read_text())["token"]


def _team() -> str:
    if t := os.environ.get("VERCEL_TEAM_ID"):
        return t
    return json.loads((_CLI / "config.json").read_text())["currentTeam"]


async def create_box(name: str) -> dict:
    """Boot a repo-less devbox, install devboxd, block until READY.

    One call (`setup: true` + a sandbox spec opts into the server-side flow).
    Cold boot is ~1 minute. Returns {"id", "url"} — url is the box's devboxd
    origin, used for the watch and tty websockets.

    Provisioning flakes happen (e.g. devboxd_setup_failed 502 when the fresh
    sandbox can't read its CA bundle); a failed create just leaves an ERROR
    row behind (names aren't unique), so retry with a new sandbox.
    """
    async with httpx.AsyncClient(timeout=600) as http:
        for attempt in range(3):
            r = await http.post(
                f"{API}/v2/devbox/create",
                params={"teamId": _team()},
                headers={"Authorization": f"Bearer {token()}"},
                json={"name": name, "setup": True, "sandbox": {}},
            )
            if r.status_code < 500 or attempt == 2:
                box = _checked(r).json()
                return {"id": box["id"], "url": box["url"]}
            await asyncio.sleep(2)


async def create_taskset(title: str) -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"{API}/v1/tasksets",
            headers={"Authorization": f"Bearer {token()}"},
            json={"title": title},
        )
        return _checked(r).json()["set_id"]


async def create_task(box_id: str, set_id: str, prompt: str) -> dict:
    """Start a claude-code task on the box: {task_id, session_id, state}.

    session_id names the box pty session the agent runs in. A fresh box can
    409 for a moment right after create (registration settles just behind
    the blocking create call), so retry briefly.
    """
    body = {"devbox_id": box_id, "set_id": set_id, "assistant": "claude-code", "prompt": prompt}
    if url := os.environ.get("DEVBOX_WEBHOOK_URL"):  # deployed mode; useless on localhost
        body["webhooks"] = [{"url": url}]
    async with httpx.AsyncClient(timeout=60) as http:
        for attempt in range(4):
            r = await http.post(
                f"{API}/v1/tasks", headers={"Authorization": f"Bearer {token()}"}, json=body
            )
            if r.status_code != 409 or attempt == 3:
                return _checked(r).json()
            await asyncio.sleep(3)


async def get_task(task_id: str) -> dict:
    """The durable task row (state + result), box-independent.

    Synced asynchronously from the box: right after a state change the row can
    lag the watch websocket by seconds. Treat pushed frames as the truth on
    state; use the row for what only it has (error reason, pr urls).
    """
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"{API}/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {token()}"}
        )
        r.raise_for_status()
        return r.json()


async def watch(box_url: str, task_id: str, quiet_after: float = 45):
    """Yield task frames from the box's watch websocket until it closes.

    Frames are {taskId, ts, body: {assistantEvent | stateTransition}}. The
    server replays the event log, then streams live; a terminal
    stateTransition is the final frame, after which the socket closes.

    Yields None after `quiet_after` seconds without a frame so the caller
    can emit a keepalive — an SSE that goes silent for minutes (coder deep
    in a long step) gets severed by intermediate proxies.
    """
    url = box_url.replace("https://", "wss://") + f"/tasks/{task_id}/watch"
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {token()}"}
    ) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=quiet_after)
            except TimeoutError:
                yield None
                continue
            except websockets.ConnectionClosed:
                return
            yield json.loads(raw)
