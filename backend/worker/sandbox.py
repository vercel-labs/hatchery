"""Thin Vercel Sandbox adapter. SDK handles never escape this module."""

import dataclasses
import os

import httpx
from vercel import sandbox as vercel_sandbox

from worker import models
from worker.daemon import main as daemon_main

DAEMON_PORT = 8787
DAEMON_PATH = "/opt/hatchery/daemon.py"
DAEMON_STATE_PATH = "/opt/hatchery/daemon-state.json"


@dataclasses.dataclass(frozen=True)
class Provisioned:
    sandbox_name: str
    routes: list[models.Route]


async def provision(
    worker_id: str, spec: models.WorkerSpec, daemon_token: str
) -> Provisioned:
    name = f"hatchery-{worker_id}"
    source = None
    if spec.repos:
        revision = spec.git_sha or spec.branch
        source = vercel_sandbox.GitSource(
            url=f"https://github.com/{spec.repos[0]}.git",
            revision=revision,
        )
    resources = None
    if spec.vcpus is not None or spec.memory is not None:
        resources = vercel_sandbox.SandboxResources(vcpus=spec.vcpus, memory=spec.memory)
    box, created = await vercel_sandbox.get_or_create_sandbox(
        name=name,
        source=source,
        ports=list(dict.fromkeys([*spec.ports, DAEMON_PORT])),
        resources=resources,
        persistent=True,
        tags={"hatchery-worker": worker_id},
    )
    if created:
        await _bootstrap(box, worker_id, spec, daemon_token)
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    daemon_route = next((route for route in routes if route.port == DAEMON_PORT), None)
    if daemon_route is None:
        raise RuntimeError("sandbox did not expose the daemon route")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{daemon_route.url}/health",
            headers={"authorization": f"Bearer {daemon_token}"},
        )
        response.raise_for_status()
        health = response.json()
    if health != {"ok": True, "version": daemon_main.VERSION}:
        raise RuntimeError("sandbox daemon returned an incompatible health response")
    return Provisioned(name, routes)


async def stop(name: str) -> None:
    box = await vercel_sandbox.get_sandbox(name=name)
    await box.stop()


async def destroy(name: str) -> None:
    box = await vercel_sandbox.get_sandbox(name=name)
    await box.destroy()


async def resume(name: str, worker_id: str, spec: models.WorkerSpec, token: str) -> None:
    box = await vercel_sandbox.resume_sandbox(name=name)
    await box.create_process(
        "python3",
        [DAEMON_PATH, "--port", str(DAEMON_PORT), "--worker-id", worker_id, "--workspace", _workspace(spec), "--state", DAEMON_STATE_PATH],
        env=_daemon_env(worker_id, spec, token),
    )


async def _bootstrap(box, worker_id: str, spec: models.WorkerSpec, token: str) -> None:
    await box.fs.mkdir("/opt/hatchery")
    await box.fs.write_text(DAEMON_PATH, daemon_main.source(), mode=0o755)
    await box.run_process(
        "/bin/sh",
        ["-lc", "command -v fx >/dev/null || curl -fsSL https://fx.sh/setup.sh | bash"],
        check=True,
        capture_output=True,
    )
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
    workspace = _workspace(spec)
    await box.create_process(
        "python3",
        [DAEMON_PATH, "--port", str(DAEMON_PORT), "--worker-id", worker_id, "--workspace", workspace, "--state", DAEMON_STATE_PATH],
        env=_daemon_env(worker_id, spec, token),
    )


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
