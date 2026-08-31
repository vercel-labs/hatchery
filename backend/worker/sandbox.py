"""Thin Vercel Sandbox adapter. SDK handles never escape this module."""

import asyncio
import base64
import dataclasses
import os
import pathlib

import httpx
from vercel import sandbox as vercel_sandbox

from worker import git, models
from worker.daemon import main as daemon_main

DAEMON_PORT = 8787
SSH_PORT = 8788
DAEMON_PATH = "/opt/hatchery/daemon.py"
DAEMON_STATE_PATH = "/opt/hatchery/daemon-state.json"
GIT_RUNTIME_PATH = "/opt/hatchery/git_runtime.py"
SHIM_PATH = "/opt/hatchery/bin"

# The worker adapter exposes the retained behavioral surface while the shared
# implementation lives in worker.git and is copied into each sandbox.
configure_git_auth = git.configure_git_auth
configure_gh = git.configure_gh
run_gh = git.run_gh
parse_pr_url = git.parse_pr_url
is_pr_create = git.is_pr_create
validate_pr_url = git.validate_pr_url
find_pr_url = git.find_pr_url
record_pr = git.record_pr
git_credentials = git.git_credentials
parse_git_args = git.parse_git_args
neutralize_commit_signing = git.neutralize_commit_signing
needs_signed_push = git.needs_signed_push
push_with_signing_fallback = git.push_with_signing_fallback
scrub_git_config_env = git.scrub_git_config_env
first_unsigned_commit = git.first_unsigned_commit
sign_request = git.sign_request
is_signed_by_app = git.is_signed_by_app
origin_owner_repo = git.origin_owner_repo
redeliver_command = daemon_main.redeliver_command


@dataclasses.dataclass(frozen=True)
class Provisioned:
    sandbox_name: str
    routes: list[models.Route]


async def provision(
    worker_id: str, spec: models.WorkerSpec, daemon_token: str
) -> Provisioned:
    name = f"hatchery-{worker_id}"
    credential = await git.git_credentials()
    source = None
    if spec.repos:
        revision = spec.git_sha or spec.branch
        source = vercel_sandbox.GitSource(
            url=f"https://github.com/{spec.repos[0]}.git",
            revision=revision,
            username="x-access-token" if credential else None,
            password=credential,
        )
    resources = None
    if spec.vcpus is not None or spec.memory is not None:
        resources = vercel_sandbox.SandboxResources(vcpus=spec.vcpus, memory=spec.memory)
    box, created = await vercel_sandbox.get_or_create_sandbox(
        name=name,
        source=source,
        ports=list(dict.fromkeys([*spec.ports, DAEMON_PORT, SSH_PORT])),
        resources=resources,
        persistent=True,
        network_policy=git.github_network_policy(credential),
        tags={"hatchery-worker": worker_id},
    )
    process = None
    if created:
        process = await _bootstrap(box, worker_id, spec, daemon_token)
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(box, worker_id, spec, daemon_token, routes, process=process)
    return Provisioned(name, routes)


async def stop(name: str) -> None:
    box = await vercel_sandbox.get_sandbox(name=name)
    await box.stop()


async def destroy(name: str) -> None:
    box = await vercel_sandbox.get_sandbox(name=name)
    await box.destroy()


async def resume(name: str, worker_id: str, spec: models.WorkerSpec, token: str) -> None:
    box = await vercel_sandbox.resume_sandbox(name=name)
    await box.update_network_policy(git.github_network_policy(await git.git_credentials()))
    await git.configure(box)
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(box, worker_id, spec, token, routes)


async def resume_for_command(record: models.Worker) -> None:
    """Resume and verify a stopped worker before its durable command is published."""
    await resume(record.sandbox_name, record.id, record.spec, record.daemon_token)


async def recover_daemon(record: models.Worker) -> None:
    """Repair daemon control and let its persisted active-task set resume fx."""
    box = await vercel_sandbox.get_sandbox(name=record.sandbox_name)
    await repair_daemon(box, record.id, record.spec, record.daemon_token, record.routes)


async def repair_daemon(
    box,
    worker_id: str,
    spec: models.WorkerSpec,
    token: str,
    routes: list[models.Route],
    *,
    process=None,
) -> None:
    daemon_route = next((route for route in routes if route.port == DAEMON_PORT), None)
    if daemon_route is None:
        raise RuntimeError("sandbox did not expose the daemon route")
    if process is None:
        try:
            health = await _daemon_health(daemon_route.url, token)
            if health == {"ok": True, "version": daemon_main.VERSION}:
                return
        except (httpx.HTTPError, ValueError):
            pass
        await box.fs.mkdir("/opt/hatchery")
        await box.fs.write_text(DAEMON_PATH, daemon_main.source(), mode=0o755)
        process = await _start_daemon(box, worker_id, spec, token)
    health = await _wait_for_daemon(daemon_route.url, token, process)
    if health != {"ok": True, "version": daemon_main.VERSION}:
        raise RuntimeError("sandbox daemon returned an incompatible health response")


async def _bootstrap(box, worker_id: str, spec: models.WorkerSpec, token: str):
    await box.fs.mkdir("/opt/hatchery")
    await box.fs.mkdir(SHIM_PATH)
    await box.fs.write_text(DAEMON_PATH, daemon_main.source(), mode=0o755)
    await box.fs.write_text(GIT_RUNTIME_PATH, pathlib.Path(git.__file__).read_text(encoding="utf-8"), mode=0o755)
    shim = f"#!/bin/sh\nexec python3 {GIT_RUNTIME_PATH} $(basename \"$0\") \"$@\"\n"
    await box.fs.write_text(f"{SHIM_PATH}/git", shim, mode=0o755)
    await box.fs.write_text(f"{SHIM_PATH}/gh", shim, mode=0o755)
    await box.run_process(
        "/bin/sh",
        [
            "-lc",
            "set -e; python3 -c 'from vercel import queue; import asyncssh, websockets' 2>/dev/null || "
            "python3 -m pip install --disable-pip-version-check 'vercel-queue==0.7.3' "
            "'asyncssh>=2.21,<3' 'websockets>=15,<17'; "
            "command -v gh >/dev/null || true; "
            "command -v fx >/dev/null || curl -fsSL https://fx.sh/setup.sh | bash",
        ],
        check=True,
        capture_output=True,
    )
    await git.configure(box)
    for repo in spec.repos[1:]:
        await box.run_process(
            "git",
            ["clone", f"https://github.com/{repo}.git", f"/vercel/sandbox/{repo.split('/')[-1]}"],
            check=True,
            capture_output=True,
        )
    if spec.setup_script:
        await box.run_process(
            "/bin/sh",
            ["-lc", spec.setup_script],
            check=True,
            capture_output=True,
        )
    return await _start_daemon(box, worker_id, spec, token)


async def _start_daemon(box, worker_id: str, spec: models.WorkerSpec, token: str):
    return await box.create_process(
        "python3",
        [DAEMON_PATH, "--port", str(DAEMON_PORT), "--worker-id", worker_id, "--workspace", _workspace(spec), "--state", DAEMON_STATE_PATH],
        env=_daemon_env(worker_id, spec, token),
    )


async def probe_route(record: models.Worker, port: int, path: str = "/") -> httpx.Response:
    """Probe one declared application route; internal control routes are not exposed."""
    if port not in record.spec.ports:
        raise ValueError("port is not declared by this worker")
    route = next((item for item in record.routes if item.port == port), None)
    if route is None:
        raise RuntimeError("sandbox route is unavailable")
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(f"{route.url.rstrip('/')}/{path.lstrip('/')}")
        response.raise_for_status()
        return response


async def snapshot(record: models.Worker, snapshot_id: str | None = None) -> str:
    """Create a filesystem snapshot, or restore one and restart the daemon."""
    box = await vercel_sandbox.get_sandbox(name=record.sandbox_name)
    if snapshot_id is None:
        created = await box.snapshot()
        return created.id
    await box.stop()
    await box.update(current_snapshot_id=snapshot_id)
    box = await vercel_sandbox.resume_sandbox(name=record.sandbox_name)
    await box.update_network_policy(git.github_network_policy(await git.git_credentials()))
    await git.configure(box)
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(box, record.id, record.spec, record.daemon_token, routes)
    return snapshot_id


def daemon_url(record: models.Worker) -> str:
    route = next((route for route in record.routes if route.port == DAEMON_PORT), None)
    if route is None:
        raise RuntimeError("sandbox daemon route is unavailable")
    return route.url.rstrip("/")


def ssh(record: models.Worker) -> tuple[str, dict[str, str]]:
    """Return the authenticated SSH WebSocket endpoint for any environment."""
    route = next((route for route in record.routes if route.port == SSH_PORT), None)
    if route is None:
        raise RuntimeError("sandbox SSH route is unavailable")
    url = route.url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url.removeprefix("https://")
    elif url.startswith("http://"):
        url = "ws://" + url.removeprefix("http://")
    return url, {"authorization": f"Bearer {record.daemon_token}"}


async def tty_read(
    record: models.Worker,
    session_id: str,
    offset: int,
    cols: int,
    rows: int,
    *,
    command: list[str] | None = None,
) -> dict:
    payload = {"offset": offset, "cols": cols, "rows": rows}
    if command is not None:
        payload["command"] = command
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f"{daemon_url(record)}/tty/{session_id}/read",
            headers={"authorization": f"Bearer {record.daemon_token}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def tty_input(record: models.Worker, session_id: str, data: bytes) -> None:
    await _tty_action(record, session_id, "input", {"data": base64.b64encode(data).decode()})


async def tty_resize(record: models.Worker, session_id: str, cols: int, rows: int) -> None:
    await _tty_action(record, session_id, "resize", {"cols": cols, "rows": rows})


async def tty_signal(record: models.Worker, session_id: str, signal_name: str) -> None:
    try:
        await _tty_action(record, session_id, "signal", {"signal": signal_name})
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise


async def _tty_action(record: models.Worker, session_id: str, action: str, payload: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{daemon_url(record)}/tty/{session_id}/{action}",
            headers={"authorization": f"Bearer {record.daemon_token}"},
            json=payload,
        )
        response.raise_for_status()


async def _daemon_health(url: str, token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{url.rstrip('/')}/health",
            headers={"authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


async def _wait_for_daemon(url: str, token: str, process=None) -> dict:
    error = None
    for attempt in range(20):
        try:
            return await _daemon_health(url, token)
        except (httpx.HTTPError, ValueError) as current:
            error = current
            if process is not None:
                await process.refresh()
                if process.returncode is not None:
                    _, stderr = await process.communicate()
                    detail = (stderr or "").strip()
                    raise RuntimeError(
                        f"sandbox daemon exited with {process.returncode}: {detail}"
                    ) from current
            if attempt < 19:
                await asyncio.sleep(1)
    raise RuntimeError("sandbox daemon did not become healthy") from error


def _workspace(spec: models.WorkerSpec) -> str:
    if spec.repos:
        return f"/vercel/sandbox/{spec.repos[0].split('/')[-1]}"
    return "/vercel/sandbox"


def _daemon_env(worker_id: str, spec: models.WorkerSpec, token: str) -> dict[str, str]:
    env = {
        "HATCHERY_DAEMON_TOKEN": token,
        "HATCHERY_WORKER_ID": worker_id,
        "HATCHERY_WORKSPACE": _workspace(spec),
        "FX_PERMISSION_MODE": "yolo",
        "FX_AUTO_UPGRADE": "0",
    }
    for name in (
        "VERCEL_OIDC_TOKEN",
        "AI_GATEWAY_API_KEY",
        "GITHUB_CONNECTOR",
        "VERCEL_QUEUE_TOKEN",
        "VERCEL_QUEUE_BASE_URL",
        "VERCEL_REGION",
        "VERCEL_DEPLOYMENT_ID",
    ):
        if value := os.environ.get(name):
            env[name] = value
    if env.get("VERCEL_QUEUE_TOKEN") == "vc-dev-token":
        public_url = os.environ.get("HATCHERY_PUBLIC_URL", "").rstrip("/")
        if not public_url:
            raise RuntimeError(
                "HATCHERY_PUBLIC_URL is required to connect a sandbox to vercel dev"
            )
        env["VERCEL_QUEUE_BASE_URL"] = f"{public_url}/_svc/_queues"
    return env
