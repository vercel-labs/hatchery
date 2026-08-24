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
import logging
import os
import pathlib
import re
import time
import urllib.parse

import httpx
import websockets

API = os.environ.get("DEVBOX_API_URL", "https://api.vercel.com")
_CLI = pathlib.Path.home() / "Library/Application Support/com.vercel.cli"

TERMINAL_STATES = ("complete", "errored")
log = logging.getLogger("app.devbox")


def _redact(text: str) -> str:
    """Hide credentials if an upstream error reflects a callback URL."""
    for key in ("secret", "token"):
        text = re.sub(rf"(?i)({key}(?:%3D|=))[^&\s\"']+", rf"\1[redacted]", text)
    return text


async def _request(
    http: httpx.AsyncClient, method: str, url: str, operation: str, **kwargs
) -> httpx.Response:
    """Log the cloud boundary without leaking auth, prompts, or webhook secrets."""
    started = time.monotonic()
    path = urllib.parse.urlsplit(url).path
    log.info("devbox request started operation=%s method=%s path=%s", operation, method, path)
    try:
        response = await http.request(method, url, **kwargs)
    except Exception:
        log.exception(
            "devbox request failed operation=%s method=%s path=%s elapsed_ms=%d",
            operation,
            method,
            path,
            (time.monotonic() - started) * 1000,
        )
        raise
    request_id = response.headers.get("x-vercel-id") or response.headers.get("x-request-id") or "-"
    log.info(
        "devbox request finished operation=%s method=%s path=%s status=%d elapsed_ms=%d request_id=%s",
        operation,
        method,
        path,
        response.status_code,
        (time.monotonic() - started) * 1000,
        request_id,
    )
    if response.status_code >= 400:
        log.error(
            "devbox response error operation=%s status=%d request_id=%s body=%r",
            operation,
            response.status_code,
            request_id,
            _redact(response.text[:2000]),
        )
    return response


def webhook_url() -> str | None:
    if configured := os.environ.get("DEVBOX_WEBHOOK_URL"):
        return configured
    if os.environ.get("VERCEL_ENV") in ("preview", "production") and (
        host := os.environ.get("VERCEL_URL")
    ):
        return f"https://{host}/channels/v1/devbox"
    return None


def tty_url(box_url: str, session_id: str, offset: str, cols: str, rows: str) -> str:
    query = urllib.parse.urlencode(
        {
            "token": token(),
            "sessionId": session_id,
            "offset": offset,
            "cols": cols,
            "rows": rows,
        }
    )
    return box_url.replace("https://", "wss://") + f"/__tty?{query}"


def _checked(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:  # keep the body; api-devbox errors are useful
        body = _redact(r.text[:300])
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {body}")
    return r


def token() -> str:
    if t := os.environ.get("VERCEL_TOKEN"):
        log.debug("devbox auth source=environment")
        return t
    path = _CLI / "auth.json"
    log.debug("devbox auth source=cli path=%s", path)
    try:
        return json.loads(path.read_text())["token"]
    except Exception:
        log.exception("devbox auth unavailable source=cli path=%s", path)
        raise


def _team() -> str:
    if t := os.environ.get("VERCEL_TEAM_ID"):
        log.debug("devbox team source=environment team_id=%s", t)
        return t
    path = _CLI / "config.json"
    log.debug("devbox team source=cli path=%s", path)
    try:
        team = json.loads(path.read_text())["currentTeam"]
    except Exception:
        log.exception("devbox team unavailable source=cli path=%s", path)
        raise
    log.debug("devbox team resolved source=cli team_id=%s", team)
    return team


async def create_box(name: str) -> dict:
    """Boot a repo-less devbox, install devboxd, block until READY.

    One call (`setup: true` + a sandbox spec opts into the server-side flow).
    Cold boot is ~1 minute. Returns {"id", "url"} — url is the box's devboxd
    origin, used for the watch and tty websockets.

    Provisioning flakes happen (e.g. devboxd_setup_failed 502 when the fresh
    sandbox can't read its CA bundle); a failed create just leaves an ERROR
    row behind (names aren't unique), so retry with a new sandbox.
    """
    team = _team()
    log.info("devbox create starting name=%s team_id=%s api=%s", name, team, API)
    async with httpx.AsyncClient(timeout=600) as http:
        for attempt in range(3):
            r = await _request(
                http,
                "POST",
                f"{API}/v2/devbox/create",
                "create_box",
                params={"teamId": team},
                headers={"Authorization": f"Bearer {token()}"},
                json={"name": name, "setup": True, "sandbox": {}},
            )
            if r.status_code < 500 or attempt == 2:
                box = _checked(r).json()
                log.info("devbox create complete name=%s box_id=%s", name, box.get("id"))
                return {"id": box["id"], "url": box["url"]}
            log.warning(
                "devbox create retrying name=%s attempt=%d status=%d", name, attempt + 1, r.status_code
            )
            await asyncio.sleep(2)


async def create_taskset(title: str) -> str:
    log.info("devbox taskset create starting title=%s", title)
    async with httpx.AsyncClient(timeout=30) as http:
        r = await _request(
            http,
            "POST",
            f"{API}/v1/tasksets",
            "create_taskset",
            headers={"Authorization": f"Bearer {token()}"},
            json={"title": title},
        )
        set_id = _checked(r).json()["set_id"]
        log.info("devbox taskset create complete set_id=%s", set_id)
        return set_id


async def create_task(
    box_id: str,
    set_id: str,
    prompt: str,
    webhook_secret: str | None = None,
    webhook_task_id: str | None = None,
) -> dict:
    """Start a claude-code task on the box: {task_id, session_id, state}.

    session_id names the box pty session the agent runs in. A fresh box can
    409 for a moment right after create (registration settles just behind
    the blocking create call), so retry briefly. In deployed mode the callback
    secret rides in the URL because devbox task webhooks do not support custom
    headers or signatures.
    """
    body = {"devbox_id": box_id, "set_id": set_id, "assistant": "claude-code", "prompt": prompt}
    url = webhook_url()
    log.info(
        "devbox task create starting box_id=%s set_id=%s launch_id=%s webhook=%s prompt_chars=%d",
        box_id,
        set_id,
        webhook_task_id or "-",
        bool(url),
        len(prompt),
    )
    if url:
        if not webhook_secret:
            raise RuntimeError("deployed task webhooks require a per-task secret")
        if not webhook_task_id:
            raise RuntimeError("DEVBOX_WEBHOOK_URL requires the owning launch id")
        query = urllib.parse.urlencode({"launch_id": webhook_task_id, "secret": webhook_secret})
        separator = "&" if urllib.parse.urlsplit(url).query else "?"
        body["webhooks"] = [{"url": f"{url}{separator}{query}"}]
    async with httpx.AsyncClient(timeout=60) as http:
        for attempt in range(4):
            r = await _request(
                http,
                "POST",
                f"{API}/v1/tasks",
                "create_task",
                headers={"Authorization": f"Bearer {token()}"},
                json=body,
            )
            if r.status_code != 409 or attempt == 3:
                created = _checked(r).json()
                log.info(
                    "devbox task create complete box_id=%s launch_id=%s task_id=%s session_id=%s state=%s",
                    box_id,
                    webhook_task_id or "-",
                    created.get("task_id"),
                    created.get("session_id"),
                    created.get("state"),
                )
                return created
            log.warning(
                "devbox task create retrying box_id=%s launch_id=%s attempt=%d status=%d",
                box_id,
                webhook_task_id or "-",
                attempt + 1,
                r.status_code,
            )
            await asyncio.sleep(3)


async def get_task(task_id: str) -> dict:
    """The durable task row (state + result), box-independent.

    Synced asynchronously from the box: right after a state change the row can
    lag the watch websocket by seconds. Treat pushed frames as the truth on
    state; use the row for what only it has (error reason, pr urls).
    """
    async with httpx.AsyncClient(timeout=30) as http:
        r = await _request(
            http,
            "GET",
            f"{API}/v1/tasks/{task_id}",
            "get_task",
            headers={"Authorization": f"Bearer {token()}"},
        )
        row = _checked(r).json()
        log.info("devbox task fetched task_id=%s state=%s", task_id, row.get("state"))
        return row


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
    log.info("devbox watch connecting task_id=%s host=%s", task_id, urllib.parse.urlsplit(url).netloc)
    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {token()}"}
        ) as ws:
            log.info("devbox watch connected task_id=%s", task_id)
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=quiet_after)
                except TimeoutError:
                    log.debug("devbox watch quiet task_id=%s quiet_after=%s", task_id, quiet_after)
                    yield None
                    continue
                except websockets.ConnectionClosed as error:
                    log.info(
                        "devbox watch closed task_id=%s code=%s reason=%s",
                        task_id,
                        error.code,
                        error.reason,
                    )
                    return
                frame = json.loads(raw)
                body = frame.get("body") or {}
                transition = body.get("stateTransition") or {}
                event = body.get("assistantEvent") or {}
                log.info(
                    "devbox watch frame task_id=%s transition=%s event=%s ts=%s",
                    task_id,
                    transition.get("to") or "-",
                    event.get("name") or "-",
                    frame.get("ts") or "-",
                )
                yield frame
    except Exception:
        log.exception("devbox watch failed task_id=%s host=%s", task_id, urllib.parse.urlsplit(url).netloc)
        raise
