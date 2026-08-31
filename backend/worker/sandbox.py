"""Thin Vercel Sandbox adapter. SDK handles never escape this module."""

import asyncio
import dataclasses
import os
import pathlib

import httpx
from vercel import sandbox as vercel_sandbox
from vercel.oidc import aio as vercel_oidc

from worker import git, models
from worker.daemon import main as daemon_main

DAEMON_PORT = 8787
SSH_PORT = 8788
DAEMON_PATH = "/opt/hatchery/daemon.py"
DAEMON_STATE_PATH = "/opt/hatchery/daemon-state.json"
DAEMON_LOG_PATH = "/opt/hatchery/daemon.log"
GIT_RUNTIME_PATH = "/opt/hatchery/git_runtime.py"
SHIM_PATH = "/opt/hatchery/bin"
AI_GATEWAY_HOST = "ai-gateway.vercel.sh"
AI_GATEWAY_PLACEHOLDER = "sandbox-network-policy-placeholder"
QUEUE_TOKEN_PLACEHOLDER = "sandbox-queue-policy-placeholder"
EXECUTION_TIME_LIMIT = 24 * 60 * 60

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
    network_policy = await _network_policy(credential, os.environ.get("VERCEL_REGION"))
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
        execution_time_limit=EXECUTION_TIME_LIMIT,
        network_policy=network_policy,
        env={"AI_GATEWAY_API_KEY": AI_GATEWAY_PLACEHOLDER},
        tags={"hatchery-worker": worker_id},
    )
    await box.update(execution_time_limit=EXECUTION_TIME_LIMIT)
    await box.update_network_policy(await _network_policy(credential, box.region))
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
    await box.update_network_policy(
        await _network_policy(await git.git_credentials(), box.region)
    )
    await git.configure(box)
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(box, worker_id, spec, token, routes)


async def prepare_for_command(record: models.Worker) -> None:
    """Acquire a live session, rotate Queue auth, and verify the daemon."""
    box = await vercel_sandbox.resume_sandbox(name=record.sandbox_name)
    await box.update(execution_time_limit=EXECUTION_TIME_LIMIT)
    await box.update_network_policy(
        await _network_policy(await git.git_credentials(), box.region)
    )
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(
        box, record.id, record.spec, record.daemon_token, routes
    )


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
            if (
                health.get("ok") is True
                and health.get("version") == daemon_main.VERSION
                and health.get("queue_connected") is True
                and health.get("event_deployment") == os.environ.get("VERCEL_DEPLOYMENT_ID")
            ):
                return
        except (httpx.HTTPError, ValueError):
            pass
        await box.fs.mkdir("/opt/hatchery")
        await box.fs.write_text(DAEMON_PATH, daemon_main.source(), mode=0o755)
        process = await _start_daemon(box, worker_id, spec, token)
    health = await _wait_for_daemon(daemon_route.url, token, process)
    if health.get("ok") is not True or health.get("version") != daemon_main.VERSION:
        raise RuntimeError("sandbox daemon returned an incompatible health response")
    if health.get("queue_connected") is not True:
        raise RuntimeError(
            f"sandbox daemon Queue connection failed: {health.get('queue_error') or 'not connected'}"
        )


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
            "set -e; python3 -c 'from vercel import connect, queue; import asyncssh, websockets' 2>/dev/null || "
            "python3 -m pip install --disable-pip-version-check 'vercel-queue==0.7.3' "
            "'vercel-connect' 'asyncssh>=2.21,<3' 'websockets>=15,<17'; "
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
            ["clone", f"https://github.com/{repo}.git", f"/vercel/{repo.split('/')[-1]}"],
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
    command = " ".join([
        "exec python3",
        DAEMON_PATH,
        "--port", str(DAEMON_PORT),
        "--worker-id", worker_id,
        "--workspace", _workspace(spec),
        "--state", DAEMON_STATE_PATH,
    ])
    return await box.create_process(
        "/bin/sh",
        [
            "-lc",
            f"pkill -f '^python3 {DAEMON_PATH}( |$)' 2>/dev/null || true; "
            f"{command} >>{DAEMON_LOG_PATH} 2>&1",
        ],
        env=_daemon_env(worker_id, spec, token, region=box.region),
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
    await box.update_network_policy(
        await _network_policy(await git.git_credentials(), box.region)
    )
    await git.configure(box)
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(box, record.id, record.spec, record.daemon_token, routes)
    return snapshot_id


async def _network_policy(github_token: str | None, region: str | None):
    oidc_token = await vercel_oidc.get_vercel_oidc_token()
    allow = dict(git.github_network_policy(github_token).allow)
    allow[AI_GATEWAY_HOST] = (
        vercel_sandbox.NetworkPolicyRule(
            transform=[
                vercel_sandbox.NetworkPolicyTransform(
                    headers={
                        "Authorization": f"Bearer {oidc_token}",
                        "ai-gateway-auth-method": "oidc",
                    }
                )
            ]
        ),
    )
    if region:
        allow[f"{region}.vercel-queue.com"] = (
            vercel_sandbox.NetworkPolicyRule(
                match=vercel_sandbox.NetworkPolicyRequestMatcher(
                    headers=[
                        vercel_sandbox.NetworkPolicyKeyValueMatcher(
                            key=vercel_sandbox.NetworkPolicyMatcher.exact("authorization"),
                            value=vercel_sandbox.NetworkPolicyMatcher.exact(
                                f"Bearer {QUEUE_TOKEN_PLACEHOLDER}"
                            ),
                        )
                    ]
                ),
                transform=[
                    vercel_sandbox.NetworkPolicyTransform(
                        headers={"Authorization": f"Bearer {oidc_token}"}
                    )
                ],
            ),
        )
    return vercel_sandbox.NetworkPolicy.custom(allow)


def daemon_url(record: models.Worker) -> str:
    route = next((route for route in record.routes if route.port == DAEMON_PORT), None)
    if route is None:
        raise RuntimeError("sandbox daemon route is unavailable")
    return route.url.rstrip("/")


def _websocket_route(url: str) -> str:
    url = url.rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url.removeprefix("https://")
    if url.startswith("http://"):
        return "ws://" + url.removeprefix("http://")
    return url


def ssh(record: models.Worker) -> tuple[str, dict[str, str]]:
    """Return the authenticated SSH WebSocket endpoint for any environment."""
    route = next((route for route in record.routes if route.port == SSH_PORT), None)
    if route is None:
        raise RuntimeError("sandbox SSH route is unavailable")
    return _websocket_route(route.url), {
        "authorization": f"Bearer {record.daemon_token}"
    }


def tty(record: models.Worker) -> tuple[str, dict[str, str]]:
    """Return the daemon's authenticated streaming TTY endpoint."""
    url, headers = ssh(record)
    return url + "/tty", headers


async def daemon_health(record: models.Worker) -> dict:
    return await _daemon_get(record, "/health")


async def tty_sessions(record: models.Worker) -> list[dict]:
    response = await _daemon_get(record, "/tty")
    return list(response.get("sessions") or [])


async def _daemon_get(record: models.Worker, path: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{daemon_url(record)}{path}",
            headers={"authorization": f"Bearer {record.daemon_token}"},
        )
        response.raise_for_status()
        return response.json()


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
            health = await _daemon_health(url, token)
            if health.get("queue_connected") is True:
                return health
            error = RuntimeError(
                health.get("queue_error") or "sandbox daemon Queue is not connected"
            )
        except (httpx.HTTPError, ValueError) as current:
            error = current
        if process is not None:
            await process.refresh()
            if process.returncode is not None:
                _, stderr = await process.communicate()
                detail = (stderr or "").strip()
                raise RuntimeError(
                    f"sandbox daemon exited with {process.returncode}: {detail}"
                ) from error
        if attempt < 19:
            await asyncio.sleep(1)
    raise RuntimeError("sandbox daemon did not become Queue-ready") from error


def _workspace(spec: models.WorkerSpec) -> str:
    if spec.repos:
        return f"/vercel/{spec.repos[0].split('/')[-1]}"
    return "/vercel"


def _daemon_env(
    worker_id: str, spec: models.WorkerSpec, token: str, *, region: str | None = None
) -> dict[str, str]:
    env = {
        "HATCHERY_DAEMON_TOKEN": token,
        "HATCHERY_WORKER_ID": worker_id,
        "HATCHERY_WORKSPACE": _workspace(spec),
        "FX_PERMISSION_MODE": "yolo",
        "FX_AUTO_UPGRADE": "0",
        "AI_GATEWAY_API_KEY": AI_GATEWAY_PLACEHOLDER,
        "VERCEL_QUEUE_TOKEN": QUEUE_TOKEN_PLACEHOLDER,
    }
    for name in (
        "GITHUB_CONNECTOR",
        "VERCEL_QUEUE_BASE_URL",
        "VERCEL_REGION",
    ):
        if value := os.environ.get(name):
            env[name] = value
    if deployment := os.environ.get("VERCEL_DEPLOYMENT_ID"):
        env["HATCHERY_EVENT_DEPLOYMENT"] = deployment
    if os.environ.get("VERCEL_QUEUE_TOKEN") == "vc-dev-token":
        env["VERCEL_QUEUE_TOKEN"] = "vc-dev-token"
    if region and "VERCEL_REGION" not in env:
        env["VERCEL_REGION"] = region
    if env.get("VERCEL_QUEUE_TOKEN") == "vc-dev-token":
        public_url = os.environ.get("HATCHERY_PUBLIC_URL", "").rstrip("/")
        if not public_url:
            raise RuntimeError(
                "HATCHERY_PUBLIC_URL is required to connect a sandbox to vercel dev"
            )
        env["VERCEL_QUEUE_BASE_URL"] = f"{public_url}/_svc/_queues"
    return env
