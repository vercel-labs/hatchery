"""Thin client for the devbox platform: the worker layer.

A devbox runs a real coding agent (claude code) in a durable pty session per
task. Task events arrive through per-task webhooks. The pty is attachable over
the box's /__tty websocket — that's the UI's terminal pane.

Auth is a Vercel user bearer. Locally we fall back to the vercel CLI's
auth.json so `vc login` is all the setup there is.
"""

import asyncio
import base64
import json
import os
import pathlib
import urllib.parse

import httpx
import websockets

API = os.environ.get("DEVBOX_API_URL", "https://api.vercel.com")
_CLI = pathlib.Path.home() / "Library/Application Support/com.vercel.cli"

TERMINAL_STATES = ("complete", "errored")
DEFAULT_MODEL = "openai/gpt-5.6-sol"


def webhook_url() -> str:
    return os.environ["HATCHERY_PUBLIC_URL"].rstrip("/") + "/channels/v1/devbox"


def tty_url(
    box_url: str,
    session_id: str | None,
    offset: str,
    cols: str,
    rows: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "token": token(),
            **({"sessionId": session_id, "offset": offset} if session_id else {}),
            "cols": cols,
            "rows": rows,
        }
    )
    return box_url.replace("https://", "wss://") + f"/__tty?{query}"


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


async def create_box(
    name: str,
    repos: list[str],
    setup_script: str | None = None,
    ports: list[int] | None = None,
    branch: str | None = None,
    git_sha: str | None = None,
) -> dict:
    """Boot a devbox, clone its repos, run setup, and block until READY.

    branch and git_sha pin the first (primary) repo; additional repos use their
    default branches. One call (`setup: true` + a sandbox spec) opts into the
    server-side flow.
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
                json={
                    "projectId": os.environ["VERCEL_PROJECT_ID"],
                    "name": name,
                    "setup": True,
                    "sandbox": {**({"ports": ports} if ports else {})},
                    "cloneRepos": repos,
                    **({"branch": branch} if branch else {}),
                    **({"gitSha": git_sha} if git_sha else {}),
                    **(
                        {"config": {"run": {"postCreateCommand": setup_script}}}
                        if setup_script
                        else {}
                    ),
                },
            )
            if r.status_code < 500 or attempt == 2:
                box = _checked(r).json()
                break
            await asyncio.sleep(2)

        for _ in range(300):
            r = await http.get(
                f"{API}/v1/devbox/{box['id']}",
                headers={"Authorization": f"Bearer {token()}"},
            )
            current = _checked(r).json()
            if current.get("state") == "READY" and current.get("sandboxUrl"):
                return {"id": box["id"], "url": current["sandboxUrl"]}
            if current.get("state") in ("ERROR", "DELETED", "STOPPED"):
                reason = current.get("statusDetails") or current.get("errorMessage") or current["state"]
                raise RuntimeError(f"devbox setup failed: {reason}")
            await asyncio.sleep(2)
        raise RuntimeError("devbox setup did not become ready within 10 minutes")


async def create_taskset(title: str) -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"{API}/v1/tasksets",
            headers={"Authorization": f"Bearer {token()}"},
            json={"title": title},
        )
        return _checked(r).json()["set_id"]


async def create_task(
    box_id: str,
    set_id: str,
    prompt: str,
    webhook_secret: str,
    webhook_task_id: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Start an fx task on the box: {task_id, session_id, state}.

    session_id names the box pty session the agent runs in. A fresh box can
    409 for a moment right after create (registration settles just behind
    the blocking create call), so retry briefly. The callback secret rides in
    the URL because DevBox task webhooks do not support custom headers or
    signatures.
    """
    url = webhook_url()
    query = urllib.parse.urlencode({"launch_id": webhook_task_id, "secret": webhook_secret})
    body = {
        "devbox_id": box_id,
        "set_id": set_id,
        "assistant": "fx",
        "model": model,
        "prompt": prompt,
        "webhooks": [{"url": f"{url}?{query}"}],
    }
    async with httpx.AsyncClient(timeout=60) as http:
        for attempt in range(4):
            r = await http.post(
                f"{API}/v1/tasks", headers={"Authorization": f"Bearer {token()}"}, json=body
            )
            if r.status_code != 409 or attempt == 3:
                return _checked(r).json()
            await asyncio.sleep(3)


async def get_task(task_id: str) -> dict:
    """Read the control-plane task, including its durable PTY session id."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"{API}/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {token()}"}
        )
        return _checked(r).json()


async def send_task_prompt(task_id: str, prompt: str) -> dict:
    """Deliver more input to an existing task without retrying.

    The endpoint may wake a sleeping devbox and can take several minutes. A
    failed request is not retried because accepted prompts are not idempotent.
    """
    async with httpx.AsyncClient(timeout=300) as http:
        r = await http.post(
            f"{API}/v1/tasks/{task_id}/prompt",
            headers={"Authorization": f"Bearer {token()}"},
            json={"prompt": prompt},
        )
        return _checked(r).json()


async def delete_task(task_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.delete(
            f"{API}/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {token()}"}
        )
        _checked(r)


async def delete_box(box_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.delete(
            f"{API}/v1/devbox/{box_id}", headers={"Authorization": f"Bearer {token()}"}
        )
        _checked(r)


async def send_tty_input(
    box_url: str,
    session_id: str,
    data: bytes,
    followup: bytes | None = None,
) -> None:
    url = tty_url(box_url, session_id, "0", "80", "24")
    async with websockets.connect(url, max_size=None) as ws:
        while True:
            frame = json.loads(await ws.recv())
            if frame.get("type") == "handshake":
                break

        async def send(value: bytes) -> None:
            await ws.send(
                json.dumps(
                    {
                        "type": "tty-input",
                        "body": {"data": base64.b64encode(value).decode()},
                    }
                )
            )

        await send(data)
        if followup is not None:
            await asyncio.sleep(0.75)
            await send(followup)
