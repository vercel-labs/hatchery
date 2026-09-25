"""The one Vercel Sandbox adapter. SDK handles never escape this module.

Two layers share it. Chat sandboxes (worker records with the daemon, for terminals,
SSH, and fx subagents) use `provision` and `prepare_for_command`.
`VercelSandboxProvider` is the agentmesh provider contract for agent threads and
serving; a thread sandbox is also a chat sandbox, provisioned through the first layer.
Imports and constructors perform no service calls.
"""

import asyncio
import base64
import collections.abc
import contextlib
import dataclasses
import datetime
import hashlib
import io
import math
import os
import pathlib
import re
import secrets
import shlex
import typing
import urllib.parse

import ai.experimental_telemetry
import httpx
from vercel import api as vercel_api
from vercel import sandbox as vercel_sandbox
from vercel.oidc import aio as vercel_oidc

from hatchery import connections, runtime
from hatchery.worker import dependencies, git, models, protocol, provider, store, transfer
from hatchery.worker.daemon import main as daemon_main
from hatchery.workspace import files

DAEMON_PORT = 8787
SSH_PORT = 8788
DAEMON_PATH = "/opt/hatchery/daemon.py"
DAEMON_STATE_PATH = "/opt/hatchery/daemon-state.json"
DAEMON_LOG_PATH = "/opt/hatchery/daemon.log"
GIT_RUNTIME_PATH = "/opt/hatchery/git_runtime.py"
SHIM_PATH = "/opt/hatchery/bin"
AI_GATEWAY_HOST = "ai-gateway.vercel.sh"
AI_GATEWAY_PLACEHOLDER = "sandbox-network-policy-placeholder"
GITHUB_TOKEN_PLACEHOLDER = "sandbox-network-policy-placeholder"
QUEUE_TOKEN_PLACEHOLDER = "sandbox-queue-policy-placeholder"
EXECUTION_TIME_LIMIT = 24 * 60 * 60
REPOS_PATH = f"{provider.WORKSPACE}/repos"

OWNER_TAG = "hatchery-owner"
WORKER_TAG = "hatchery-worker"
READY_MARKER = f"{provider.WORKSPACE}/.hatchery-ready"
RUNTIME_ROOT = f"{dependencies.ENVIRONMENT_ROOT}/runtime"
SERVE_REVISION = f"{dependencies.ENVIRONMENT_ROOT}/serve-revision"
SERVE_LOCK = f"{dependencies.ENVIRONMENT_ROOT}/serve.lock"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
COMMAND_STREAM_GRACE_SECONDS = 30
HTTP_OPERATION_TIMEOUT_SECONDS = 60


@dataclasses.dataclass(frozen=True)
class Provisioned:
    sandbox_name: str
    routes: list[models.Route]


async def _github_credential(
    user_id: str | None, *, required: bool = True
) -> str | None:
    if user_id is None:
        return await git.git_credentials()
    try:
        return await connections.github_token(user_id)
    except connections.ConnectionRequired as error:
        if not required:
            return None
        raise RuntimeError(
            "connect GitHub before creating a repository sandbox"
        ) from error


async def _git_identity(user_id: str | None) -> tuple[str, str] | None:
    if user_id is None:
        return None
    connection = await connections.github_identity(user_id)
    if connection is None:
        return None
    login = str(connection.get("login") or "")
    github_id = str(connection.get("id") or "")
    if not login or not github_id:
        return None
    return str(
        connection.get("name") or login
    ), f"{github_id}+{login}@users.noreply.github.com"


async def _canonicalize_repos(spec: models.WorkerSpec, credential: str | None) -> None:
    if not spec.repos or not credential:
        return
    headers = {
        "accept": "application/vnd.github+json",
        "authorization": f"Bearer {credential}",
        "x-github-api-version": "2022-11-28",
    }
    canonical = []
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers=headers,
        timeout=30,
        follow_redirects=True,
    ) as client:
        for repo in spec.repos:
            response = await client.get(f"/repos/{repo}")
            response.raise_for_status()
            canonical.append(str(response.json()["full_name"]))
    spec.repos = canonical


async def _credentials(
    spec: models.WorkerSpec, user_id: str | None, *, required: bool
) -> tuple[str | None, tuple[str, str] | None]:
    # A thread sandbox clones best effort and never waits for a GitHub connection.
    thread = spec.purpose == "thread"
    github = await _github_credential(user_id, required=required and not thread)
    identity = await _git_identity(user_id)
    if not thread:
        await _canonicalize_repos(spec, github)
    return github, identity


async def _configure_repo_remotes(box, spec: models.WorkerSpec) -> None:
    if spec.purpose == "thread":
        return  # cloned as named; a failed clone leaves no checkout to configure
    for repo in spec.repos:
        await box.run_process(
            "git",
            [
                "-C",
                f"/vercel/{repo.split('/')[-1]}",
                "remote",
                "set-url",
                "origin",
                f"https://github.com/{repo}.git",
            ],
            check=True,
            capture_output=True,
        )


async def provision(
    worker_id: str,
    spec: models.WorkerSpec,
    daemon_token: str,
    *,
    user_id: str | None = None,
) -> Provisioned:
    name = f"hatchery-{worker_id}"
    credential, identity = await _credentials(
        spec, user_id, required=bool(spec.repos)
    )
    network_policy = await _network_policy(credential, os.environ.get("VERCEL_REGION"))
    source = None
    if spec.repos and spec.purpose != "thread":
        revision = spec.git_sha or spec.branch
        source = vercel_sandbox.GitSource(
            url=f"https://github.com/{spec.repos[0]}.git",
            revision=revision,
            username="x-access-token" if credential else None,
            password=credential,
        )
    vcpus, memory = spec.resolved_resources()
    resources = vercel_sandbox.SandboxResources(vcpus=vcpus, memory=memory)
    box, created = await vercel_sandbox.get_or_create_sandbox(
        name=name,
        source=source,
        ports=list(dict.fromkeys([*spec.ports, DAEMON_PORT, SSH_PORT])),
        resources=resources,
        persistent=True,
        execution_time_limit=EXECUTION_TIME_LIMIT,
        network_policy=network_policy,
        env={"AI_GATEWAY_API_KEY": AI_GATEWAY_PLACEHOLDER},
        tags={
            "hatchery-worker": worker_id,
            "hatchery-size": spec.size,
        },
    )
    await box.update(execution_time_limit=EXECUTION_TIME_LIMIT)
    await box.update_network_policy(await _network_policy(credential, box.region))
    process = None
    if created:
        process = await _bootstrap(
            box,
            worker_id,
            spec,
            daemon_token,
            identity=identity,
        )
    routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
    await repair_daemon(box, worker_id, spec, daemon_token, routes, process=process)
    return Provisioned(name, routes)


async def is_live(name: str) -> bool:
    """Check sandbox liveness without resuming it."""
    try:
        box = await vercel_sandbox.get_sandbox(name=name)
    except Exception:
        return False
    return box.status == vercel_sandbox.SandboxStatus.RUNNING


async def stop(name: str) -> None:
    box = await vercel_sandbox.get_sandbox(name=name)
    await box.stop()


async def destroy(name: str) -> None:
    box = await vercel_sandbox.get_sandbox(name=name)
    await box.destroy()


async def prepare_for_command(
    record: models.Worker, *, actor_user_id: str | None = None
) -> None:
    """Acquire a live session, rotate Queue auth, and verify the daemon."""
    async with ai.experimental_telemetry.span("sandbox.prepare") as span:
        span.set_attrs(
            {"chat.id": record.chat_id, "worker.id": record.id},
            sandbox_name=record.sandbox_name,
            worker_state=record.status,
            sandbox_size=record.spec.size,
            sandbox_vcpus=record.spec.resolved_resources()[0],
            sandbox_memory_mb=record.spec.resolved_resources()[1],
        )
        box = await vercel_sandbox.resume_sandbox(name=record.sandbox_name)
        span.set_attrs(region=box.region or "")
        await box.update(execution_time_limit=EXECUTION_TIME_LIMIT)
        user_id = actor_user_id if actor_user_id is not None else record.user_id
        credential, identity = await _credentials(
            record.spec, user_id, required=bool(record.spec.repos)
        )
        await box.update_network_policy(await _network_policy(credential, box.region))
        await git.configure(box, identity)
        await _configure_repo_remotes(box, record.spec)
        routes = [models.Route(port=route.port, url=route.url) for route in box.routes]
        async with ai.experimental_telemetry.span("sandbox.daemon.repair") as repair:
            repair.set_attrs(
                {"worker.id": record.id}, daemon_version=daemon_main.VERSION
            )
            await repair_daemon(
                box, record.id, record.spec, record.daemon_token, routes
            )


async def prepare_for_tty(record: models.Worker) -> None:
    """Resume a sandbox without replacing the daemon that owns its TTY sessions."""
    async with ai.experimental_telemetry.span("sandbox.tty.prepare") as span:
        span.set_attrs(
            {"chat.id": record.chat_id, "worker.id": record.id},
            sandbox_name=record.sandbox_name,
        )
        box = await vercel_sandbox.resume_sandbox(name=record.sandbox_name)
        span.set_attrs(region=box.region or "")
        await box.update(execution_time_limit=EXECUTION_TIME_LIMIT)


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
                and health.get("event_deployment")
                == os.environ.get("VERCEL_DEPLOYMENT_ID")
            ):
                return
        except httpx.HTTPError, ValueError:
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


async def _bootstrap(
    box,
    worker_id: str,
    spec: models.WorkerSpec,
    token: str,
    *,
    identity: tuple[str, str] | None = None,
):
    await box.fs.mkdir("/opt/hatchery")
    await box.fs.mkdir(SHIM_PATH)
    await box.fs.write_text(DAEMON_PATH, daemon_main.source(), mode=0o755)
    await box.fs.write_text(
        GIT_RUNTIME_PATH,
        pathlib.Path(git.__file__).read_text(encoding="utf-8"),
        mode=0o755,
    )
    shim = f'#!/bin/sh\nexec python3 {GIT_RUNTIME_PATH} $(basename "$0") "$@"\n'
    await box.fs.write_text(f"{SHIM_PATH}/git", shim, mode=0o755)
    await box.fs.write_text(f"{SHIM_PATH}/gh", shim, mode=0o755)
    await box.run_process(
        "/bin/sh",
        [
            "-lc",
            "set -e; python3 -c 'from vercel import connect, queue; import asyncssh, websockets' 2>/dev/null || "
            "python3 -m pip install --disable-pip-version-check 'vercel-queue==0.7.3' "
            "'vercel-connect' 'asyncssh>=2.21,<3' 'websockets>=15,<17'; "
            "command -v gh >/dev/null || true",
        ],
        check=True,
        capture_output=True,
    )
    await git.configure(box, identity)
    if spec.purpose == "thread":
        await box.fs.mkdir(REPOS_PATH)
        for repo in spec.repos:
            # Best effort: private repos need the actor's GitHub connection.
            await box.run_process(
                "git",
                [
                    "clone",
                    f"https://github.com/{repo}.git",
                    f"{REPOS_PATH}/{repo.split('/')[-1]}",
                ],
                capture_output=True,
            )
    else:
        for repo in spec.repos[1:]:
            await box.run_process(
                "git",
                [
                    "clone",
                    f"https://github.com/{repo}.git",
                    f"/vercel/{repo.split('/')[-1]}",
                ],
                check=True,
                capture_output=True,
            )
    if spec.setup_script:
        await box.run_process(
            "/bin/sh",
            ["-lc", spec.setup_script],
            env={"GH_TOKEN": GITHUB_TOKEN_PLACEHOLDER},
            check=True,
            capture_output=True,
        )
    return await _start_daemon(box, worker_id, spec, token)


async def _start_daemon(
    box,
    worker_id: str,
    spec: models.WorkerSpec,
    token: str,
):
    command = " ".join(
        [
            "exec python3",
            DAEMON_PATH,
            "--port",
            str(DAEMON_PORT),
            "--worker-id",
            worker_id,
            "--workspace",
            _workspace(spec),
            "--state",
            DAEMON_STATE_PATH,
        ]
    )
    return await box.create_process(
        "/bin/sh",
        [
            "-lc",
            "set -e; "
            f'if [ "$({daemon_main.FX_BINARY} --version 2>/dev/null)" != "{daemon_main.FX_VERSION}" ]; then '
            f"curl -fsSL https://fx.sh/setup.sh | FX_INSTALL_DIR={SHIM_PATH} bash -s -- v{daemon_main.FX_VERSION}; fi; "
            f'test "$({daemon_main.FX_BINARY} --version)" = "{daemon_main.FX_VERSION}"; '
            f"pkill -f '^python3 {DAEMON_PATH}( |$)' 2>/dev/null || true; "
            f"{command} >>{DAEMON_LOG_PATH} 2>&1",
        ],
        env=_daemon_env(worker_id, spec, token, region=box.region),
    )


async def probe_route(
    record: models.Worker, port: int, path: str = "/"
) -> httpx.Response:
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
    credential, identity = await _credentials(
        record.spec, record.user_id, required=False
    )
    await box.update_network_policy(await _network_policy(credential, box.region))
    await git.configure(box, identity)
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
                            key=vercel_sandbox.NetworkPolicyMatcher.exact(
                                "authorization"
                            ),
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
    if spec.purpose == "thread":
        return REPOS_PATH
    if spec.repos:
        return f"/vercel/{spec.repos[0].split('/')[-1]}"
    return "/vercel"


def _daemon_env(
    worker_id: str,
    spec: models.WorkerSpec,
    token: str,
    *,
    region: str | None = None,
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


# The agentmesh provider contract (ported from agentmesh `sandbox/vercel.py`).


def _error_summary(error: BaseException) -> str:
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


class _BoundedWriter(io.StringIO):
    """Keep the first `limit` bytes of a stream; the remainder is dropped, not buffered."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit, self.size, self.truncated = limit, 0, False

    def write(self, text: str) -> int:
        room = self.limit - self.size
        if room <= 0:
            self.truncated = True
            return len(text)
        data = text.encode()
        if len(data) > room:
            text, self.truncated = data[:room].decode("utf-8", "ignore"), True
        self.size += len(text.encode())
        return super().write(text)

    def value(self) -> str:
        return self.getvalue() + ("\n[output truncated]" if self.truncated else "")


class VercelSandbox:
    def __init__(
        self,
        parent: "VercelSandboxProvider",
        remote: typing.Any,
        purpose: provider.SandboxPurpose = "thread",
    ) -> None:
        self.provider, self.remote = parent, remote
        self.name: str = remote.name
        self.purpose = purpose
        self.active = True
        self._python_environment: dependencies.PythonEnvironment | None = None
        self._runtime_paths: set[str] = set()

    async def _run(
        self,
        command: str,
        args: collections.abc.Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: collections.abc.Mapping[str, str] | None = None,
        max_output: int = MAX_OUTPUT_BYTES,
        sudo: bool = False,
        classify_command_failure: bool = False,
    ) -> provider.ExecResult:
        if not self.active:
            raise RuntimeError("sandbox is stopped; acquire it before use")
        stdout, stderr = _BoundedWriter(max_output), _BoundedWriter(max_output)
        async with self.provider.command_remote(self.remote, timeout=timeout) as remote:
            try:
                result = await remote.run_process(
                    command,
                    list(args),
                    cwd=cwd,
                    env=dict(env or {}),
                    sudo=sudo,
                    kill_after=timeout,
                    stdout=stdout,
                    stderr=stderr,
                )
            except BaseException as error:
                # A lost stream does not prove the remote command stopped. Stop its
                # session before reporting a recoverable tool error.
                self.active = False
                try:
                    await remote.stop()
                except BaseException as stop_error:
                    if (
                        classify_command_failure
                        and isinstance(error, Exception)
                        and isinstance(stop_error, Exception)
                    ):
                        raise provider.SandboxCommandUncertain(
                            "command result was lost "
                            f"({_error_summary(error)}) and sandbox stop failed "
                            f"({_error_summary(stop_error)})"
                        ) from stop_error
                    raise
                if classify_command_failure and isinstance(error, Exception):
                    raise provider.SandboxCommandStopped(
                        f"command result was lost ({_error_summary(error)}); sandbox was stopped"
                    ) from error
                raise
        return provider.ExecResult(result.returncode, stdout.value(), stderr.value())

    async def exec(
        self,
        command: str,
        *,
        timeout: float,
        env: collections.abc.Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> provider.ExecResult:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if "\x00" in command or len(command.encode()) > 65536:
            raise ValueError("command contains NUL or exceeds byte limit")
        python = await self._sync_python_environment()
        helpers = await self.install_runtime(runtime.sandbox_files())
        # Agent scripts first, then the runtime helpers, then the requirements venv.
        path = f"{provider.WORKSPACE}/self/scripts:{helpers}/scripts"
        if python.path:
            path += f":{python.path}/bin"
        environment = {
            "LANG": "C.UTF-8",
            "PYTHONPATH": f"{provider.WORKSPACE}/self:{helpers}",
        }
        if self.purpose == "thread":
            # The chat sandbox's own HOME, git/gh shims, and GitHub marker, exactly as
            # fx subagents and terminals see them; the network policy swaps the marker
            # for the actor's credential, which never enters the sandbox.
            path += f":{SHIM_PATH}"
            environment["GH_TOKEN"] = GITHUB_TOKEN_PLACEHOLDER
        else:
            environment["HOME"] = f"{dependencies.ENVIRONMENT_ROOT}/home"
        environment["PATH"] = f"{path}:/usr/local/bin:/usr/bin:/bin"
        environment.update(env or {})
        # coreutils `timeout` yields exit 124; the service-side kill is the backstop.
        input_path = None
        executable = "timeout"
        arguments = [
            "-k",
            "5",
            f"{int(timeout)}s",
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]
        if stdin is not None:
            if len(stdin) > MAX_OUTPUT_BYTES:
                raise ValueError("stdin exceeds byte limit")
            input_path = f"/tmp/hatchery-stdin-{secrets.token_hex(16)}"
            await self.remote.fs.write_bytes(input_path, stdin, mode=0o600)
            executable = "sh"
            arguments = [
                "-c",
                'input="$1"; shift; exec "$@" < "$input"',
                "hatchery-stdin",
                input_path,
                "timeout",
                *arguments,
            ]
        try:
            result = await self._run(
                executable,
                arguments,
                cwd=f"{provider.WORKSPACE}/self",
                timeout=timeout + 15,
                env=environment,
                classify_command_failure=True,
            )
        finally:
            if input_path is not None:
                with contextlib.suppress(BaseException):
                    await self.remote.fs.remove(input_path)
        if result.timed_out:
            note = f"command killed after {int(timeout)}s"
            result = provider.ExecResult(124, result.stdout, f"{result.stderr}\n{note}".strip())
        if python.warning:
            result = provider.ExecResult(
                result.exit_code,
                result.stdout,
                f"{python.warning}\n{result.stderr}".strip(),
            )
        return result

    async def _sync_python_environment(self) -> dependencies.PythonEnvironment:
        if self.purpose == "serve" and self._python_environment is not None:
            return self._python_environment
        seconds = dependencies.INSTALL_TIMEOUT_SECONDS
        result = await self._run(
            "timeout",
            ["-k", "5", f"{seconds}s", "sh", "-c", dependencies.SYNC_SCRIPT],
            cwd=provider.WORKSPACE,
            timeout=seconds + 15,
            classify_command_failure=True,
        )
        environment = dependencies.environment_from_result(
            result.exit_code, result.stdout, result.stderr
        )
        if self.purpose == "serve" and environment.path is not None:
            self._python_environment = environment
        return environment

    async def upload(self, tree: files.Tree) -> None:
        archive = f"/tmp/hatchery-upload-{secrets.token_hex(16)}.tar"
        await self.remote.fs.write_bytes(archive, transfer.pack(tree))
        result = await self._run(
            "sh",
            [
                "-c",
                f"tar -xf {shlex.quote(archive)} -C {provider.WORKSPACE} "
                f"&& rm -f {shlex.quote(archive)}",
            ],
            cwd=provider.WORKSPACE,
            timeout=120,
        )
        if result.exit_code:
            raise RuntimeError(f"sandbox upload failed: {result.stderr[:2000]}")

    async def download(self, roots: collections.abc.Sequence[str]) -> dict[str, files.File]:
        result = await self._run(
            "sh",
            [
                "-c",
                'if [ "$#" -eq 0 ]; then tar -cf - -T /dev/null; else '
                "tar --ignore-failed-read --exclude=.git --exclude=__pycache__ "
                "--exclude='*.pyc' --exclude='*.pyo' -cf - \"$@\"; fi | base64 -w0",
                "hatchery-download",
                *roots,
            ],
            cwd=provider.WORKSPACE,
            timeout=120,
            max_output=64 * 1024 * 1024,
        )
        if result.exit_code:
            raise RuntimeError(f"sandbox download failed: {result.stderr[:2000]}")
        return transfer.unpack(base64.b64decode(result.stdout, validate=True), roots)

    async def replace(self, roots: collections.abc.Sequence[str], tree: files.Tree) -> None:
        for root in roots:
            if root not in ("self", "wiki"):
                raise ValueError("only self and wiki can be replaced by memory refresh")
        selected = {
            path: file
            for path, file in tree.items()
            if any(path == root or path.startswith(f"{root}/") for root in roots)
        }
        suffix = secrets.token_hex(16)
        await self.remote.fs.write_bytes(
            f"/tmp/hatchery-replace-{suffix}.tar", transfer.pack(selected)
        )
        archive = shlex.quote(f"/tmp/hatchery-replace-{suffix}.tar")
        staging = shlex.quote(f"{dependencies.ENVIRONMENT_ROOT}/replace-{suffix}")
        quoted = " ".join(shlex.quote(root) for root in roots)
        # Each root is swapped with one rename after the archive has fully extracted.
        result = await self._run(
            "sh",
            [
                "-c",
                f"set -e; rm -rf {staging}; mkdir -p {staging}; "
                f"tar -xf {archive} -C {staging}; "
                f'for p in {quoted}; do mkdir -p {staging}/"$p"; rm -rf "$p"; '
                f'mv {staging}/"$p" "$p"; done; '
                f"rm -rf {staging} {archive}",
            ],
            cwd=provider.WORKSPACE,
            timeout=120,
        )
        if result.exit_code:
            raise RuntimeError(f"sandbox memory replace failed: {result.stderr[:2000]}")

    async def deploy(self, tree: files.Tree, revision: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("revision must be a Git commit SHA")
        if any(path != "self" and not path.startswith("self/") for path in tree):
            raise ValueError("serve deployment accepts only self files")
        suffix = secrets.token_hex(16)
        await self.remote.fs.write_bytes(
            f"/tmp/hatchery-deploy-{suffix}.tar", transfer.pack(tree), mode=0o600
        )
        archive = shlex.quote(f"/tmp/hatchery-deploy-{suffix}.tar")
        staging = shlex.quote(f"{dependencies.ENVIRONMENT_ROOT}/deploy-{suffix}")
        workspace = provider.WORKSPACE
        script = (
            f"set -e; rm -rf {staging}; mkdir -p {staging}; "
            f"tar -xf {archive} -C {staging}; "
            f"mkdir -p {staging}/self; rm -rf {workspace}/self; "
            f"mv {staging}/self {workspace}/self; "
            f"chown -R root:root {workspace}/self; chmod -R a-w {workspace}/self; "
            f"printf '%s\\n' {shlex.quote(revision)} > {SERVE_REVISION}; "
            f"rm -rf {staging} {archive}"
        )
        result = await self._run(
            "flock",
            [SERVE_LOCK, "sh", "-c", script],
            cwd=workspace,
            timeout=90,
            sudo=True,
        )
        if result.exit_code:
            raise RuntimeError(f"serve deployment failed: {result.stderr[:2000]}")
        self._python_environment = None

    async def install_runtime(
        self, runtime_files: collections.abc.Mapping[str, bytes]
    ) -> str:
        if not runtime_files:
            raise ValueError("runtime files must not be empty")
        digest = hashlib.sha256()
        for path, content in sorted(runtime_files.items()):
            if not re.fullmatch(
                r"hatchery/(?:__init__\.py|sdk/(?:__init__|__main__)\.py)"
                r"|scripts/[a-z][a-z0-9-]{0,62}",
                path,
            ):
                raise ValueError("runtime contains an unsupported path")
            if len(content) > 256 * 1024:
                raise ValueError("runtime file exceeds byte limit")
            digest.update(path.encode() + b"\0" + content)
        destination = f"{RUNTIME_ROOT}/{digest.hexdigest()}"
        if destination in self._runtime_paths:
            return destination
        marker = f"{destination}/.ready"
        if await self.remote.fs.exists(marker):
            self._runtime_paths.add(destination)
            return destination
        # Root-owned and read-only, so agent commands cannot change the helpers.
        result = await self._run("mkdir", ["-p", destination], cwd="/", timeout=30, sudo=True)
        if result.exit_code:
            raise RuntimeError(f"runtime directory installation failed: {result.stderr[:2000]}")
        for path, content in sorted(runtime_files.items()):
            temporary = f"/tmp/hatchery-runtime-{secrets.token_hex(16)}"
            await self.remote.fs.write_bytes(temporary, content, mode=0o600)
            try:
                result = await self._run(
                    "install",
                    [
                        "-D",
                        "-o",
                        "root",
                        "-g",
                        "root",
                        "-m",
                        "0555" if path.startswith("scripts/") else "0444",
                        temporary,
                        f"{destination}/{path}",
                    ],
                    cwd="/",
                    timeout=30,
                    sudo=True,
                )
            finally:
                with contextlib.suppress(BaseException):
                    await self.remote.fs.remove(temporary)
            if result.exit_code:
                raise RuntimeError(f"runtime file installation failed: {result.stderr[:2000]}")
        result = await self._run("touch", [marker], cwd="/", timeout=30, sudo=True)
        if result.exit_code:
            raise RuntimeError(f"runtime marker installation failed: {result.stderr[:2000]}")
        self._runtime_paths.add(destination)
        return destination


class VercelSandboxProvider:
    """Named persistent sandboxes for agent threads (`thread`) and serving (`serve`).

    A thread sandbox is a normal chat sandbox: a worker record owned by the chat, with
    the daemon, so terminals, SSH, and fx subagents work in it unchanged. Its worker ID
    is the name without the `hatchery-` prefix. A serve sandbox has no worker record or
    daemon and uses the resource limits given here.
    """

    def __init__(
        self,
        *,
        owner: str = "hatchery",
        network: bool = True,
        cpus: int = 1,
        memory: int = 2048,
        execution_time_limit: float = 1800,
    ) -> None:
        if not owner or cpus <= 0 or memory <= 0:
            raise ValueError("owner and positive resource limits are required")
        if not math.isfinite(execution_time_limit) or execution_time_limit <= 0:
            raise ValueError("execution_time_limit must be positive and finite")
        self.owner, self.network = owner, network
        self.cpus, self.memory = cpus, memory
        self.execution_time_limit = execution_time_limit

    @contextlib.asynccontextmanager
    async def command_remote(
        self, remote: typing.Any, *, timeout: float
    ) -> collections.abc.AsyncIterator[typing.Any]:
        """Bind streamed execution to an HTTP deadline beyond the remote kill deadline."""

        def http_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=HTTP_OPERATION_TIMEOUT_SECONDS,
                    read=timeout + COMMAND_STREAM_GRACE_SECONDS,
                    write=HTTP_OPERATION_TIMEOUT_SECONDS,
                    pool=HTTP_OPERATION_TIMEOUT_SECONDS,
                ),
                limits=httpx.Limits(max_connections=100),
                http2=False,
            )

        async with vercel_api.session(httpx_client_factory=http_client):
            current = await vercel_sandbox.get_sandbox(name=remote.name)
            self._check_owner(current, remote.name)
            if current.created_at != remote.created_at:
                raise RuntimeError(f"sandbox {remote.name!r} was replaced during command setup")
            yield current

    def _check_owner(self, remote: typing.Any, name: str) -> None:
        tags = remote.tags or {}
        if remote.name != name or (
            tags.get(OWNER_TAG) != self.owner
            and tags.get(WORKER_TAG) != name.removeprefix("hatchery-")
        ):
            raise ValueError(f"foreign Vercel sandbox name collision: {name}")
        if remote.persistent is not True:
            raise ValueError(f"Vercel sandbox is not persistent: {name}")

    async def _existing(self, name: str) -> typing.Any:
        provider.validate_name(name)
        try:
            remote = await vercel_sandbox.get_sandbox(name=name)
        except vercel_sandbox.SandboxApiError as error:
            if error.status_code == 404:
                raise FileNotFoundError("Thread sandbox is unavailable") from error
            raise
        except vercel_sandbox.SandboxCredentialsError as error:
            raise RuntimeError(
                "Vercel Sandbox authentication required: configure Vercel OIDC, or "
                "VERCEL_TOKEN, VERCEL_PROJECT_ID and VERCEL_TEAM_ID"
            ) from error
        self._check_owner(remote, name)
        return remote

    async def _resolve_entry(self, remote: typing.Any, path: str) -> typing.Any:
        """Resolve without following a symlink in an already listed path component."""
        current = provider.WORKSPACE
        found = None
        parts = pathlib.PurePosixPath(path).parts
        for index, part in enumerate(parts):
            entries = await remote.fs.listdir(current)
            found = next((entry for entry in entries if entry.path == part), None)
            if found is None:
                raise FileNotFoundError("Path is not present in this sandbox")
            if index < len(parts) - 1 and found.kind != "directory":
                raise FileNotFoundError("Path is not a directory in this sandbox")
            current = f"{current}/{part}"
        return found

    async def list_directory(self, name: str, path: str = "") -> provider.SandboxDirectory:
        provider.validate_workspace_path(path, allow_root=True)
        remote = await self._existing(name)
        if path:
            entry = await self._resolve_entry(remote, path)
            if entry.kind != "directory":
                raise FileNotFoundError("Path is not a directory in this sandbox")
        root = f"{provider.WORKSPACE}/{path}" if path else provider.WORKSPACE
        entries = await remote.fs.listdir(root)
        visible: list[provider.SandboxEntry] = []
        for entry in sorted(entries, key=lambda item: (item.kind != "directory", item.path)):
            child = f"{path}/{entry.path}" if path else entry.path
            try:
                provider.validate_workspace_path(child)
            except ValueError:
                continue
            visible.append(provider.SandboxEntry(entry.path, child, entry.kind))
        truncated = len(visible) > provider.MAX_DIRECTORY_ENTRIES
        return provider.SandboxDirectory(
            path, tuple(visible[: provider.MAX_DIRECTORY_ENTRIES]), truncated
        )

    async def read_file(self, name: str, path: str, *, limit: int) -> provider.SandboxFilePreview:
        provider.validate_workspace_path(path)
        if limit <= 0:
            raise ValueError("file preview limit must be positive")
        remote = await self._existing(name)
        entry = await self._resolve_entry(remote, path)
        if entry.kind != "file":
            raise FileNotFoundError("Path is not a regular file in this sandbox")
        reader = remote.fs.open(f"{provider.WORKSPACE}/{path}", "rb")
        async with reader:
            content = await reader.read(limit + 1)
        return provider.SandboxFilePreview(path, content[:limit], len(content) > limit)

    async def acquire(
        self,
        name: str,
        *,
        seed: provider.Seed,
        owner: str = "",
        purpose: provider.SandboxPurpose = "thread",
        chat: provider.ThreadChat | None = None,
    ) -> provider.Acquired:
        provider.validate_name(name)
        try:
            if purpose == "thread":
                remote = await self._thread_remote(name, chat)
            else:
                remote = await self._serve_remote(name)
        except vercel_sandbox.SandboxCredentialsError as error:
            raise RuntimeError(
                "Vercel Sandbox authentication required: configure Vercel OIDC, or "
                "VERCEL_TOKEN, VERCEL_PROJECT_ID and VERCEL_TEAM_ID"
            ) from error
        sandbox = VercelSandbox(self, remote, purpose)
        await remote.fs.mkdir(dependencies.ENVIRONMENT_ROOT, recursive=True)
        await remote.fs.mkdir(REPOS_PATH, recursive=True)
        await remote.fs.mkdir(f"{provider.WORKSPACE}/scratchpad", recursive=True)
        if purpose == "serve":
            await remote.fs.mkdir(f"{dependencies.ENVIRONMENT_ROOT}/home", recursive=True)
            await remote.fs.mkdir(f"{provider.WORKSPACE}/data", recursive=True)
        fresh = not await remote.fs.exists(READY_MARKER)
        if fresh:
            roots = " ".join(f"{provider.WORKSPACE}/{root}" for root in files.SANDBOX_ROOTS)
            await sandbox._run(
                "sh", ["-c", f"rm -rf {roots} && mkdir -p {roots}"], cwd="/", timeout=60
            )
            await sandbox.upload(await seed())
            protected = f"{provider.WORKSPACE}/collective"
            result = await sandbox._run(
                "sh",
                ["-c", f"chown -R root:root {protected} && chmod -R a-w {protected}"],
                cwd="/",
                timeout=60,
                sudo=True,
            )
            if result.exit_code:
                raise RuntimeError(
                    f"could not protect collective workspace: {result.stderr[:2000]}"
                )
            await remote.fs.write_text(READY_MARKER, "ready\n")
        return provider.Acquired(sandbox, fresh=fresh)

    async def _thread_remote(self, name: str, chat: provider.ThreadChat | None) -> typing.Any:
        """Create, resume, or rebuild the chat's worker record, sandbox, and daemon."""
        if chat is None:
            raise ValueError("a thread sandbox needs the chat that owns it")
        if not name.startswith("hatchery-"):
            raise ValueError("thread sandbox names must start with 'hatchery-'")
        worker_id = name.removeprefix("hatchery-")
        record = await store.get(worker_id)
        if record is not None and (
            record.chat_id != chat.id or record.spec.purpose != "thread"
        ):
            raise ValueError(f"sandbox {name} belongs to another chat")
        # A passive lookup first, so get-or-create cannot resume a foreign sandbox.
        try:
            existing = await vercel_sandbox.get_sandbox(name=name)
        except vercel_sandbox.SandboxApiError as error:
            if error.status_code != 404:
                raise
            existing = None
        else:
            self._check_owner(existing, name)
        now = datetime.datetime.now(datetime.UTC).isoformat()
        if record is None or existing is None or record.status in ("creating", "failed"):
            # New, lost, or never finished: provision it; the ready marker reseeds files.
            spec = models.WorkerSpec(title="thread", repos=list(chat.repos), purpose="thread")
            record = models.Worker(
                id=worker_id,
                chat_id=chat.id,
                user_id=record.user_id if record else chat.user_id,
                sandbox_name=name,
                command_topic=protocol.command_topic(worker_id),
                title=spec.title,
                status="creating",
                spec=spec,
                daemon_token=record.daemon_token if record else secrets.token_urlsafe(32),
                created_at=record.created_at if record else now,
                updated_at=now,
            )
            await store.save(record)
            try:
                provisioned = await provision(
                    worker_id, spec, record.daemon_token, user_id=chat.user_id
                )
            except Exception:
                record.status = "failed"
                await store.save(record)
                raise
            record.routes = provisioned.routes
            record.daemon_version = daemon_main.VERSION
        elif not (
            # A thread acquires once per tool call; a sandbox that is already running for
            # the same actor needs no resume, credential rotation, or daemon repair.
            record.status == "running"
            and getattr(existing, "status", None) == "running"  # SandboxStatus is a StrEnum
            and chat.user_id in (None, record.user_id)
        ):
            # Resume, renew the actor's network policy and git identity, repair the daemon.
            await prepare_for_command(record, actor_user_id=chat.user_id)
        record.status = "running"
        record.updated_at = now
        await store.save(record)
        remote = await vercel_sandbox.get_sandbox(name=name)
        self._check_owner(remote, name)
        return remote

    async def _serve_remote(self, name: str) -> typing.Any:
        try:
            remote = await vercel_sandbox.get_sandbox(name=name)
        except vercel_sandbox.SandboxApiError as error:
            if error.status_code != 404:
                raise
        else:
            self._check_owner(remote, name)
        network_policy = (
            vercel_sandbox.NetworkPolicy.allow_all()
            if self.network
            else vercel_sandbox.NetworkPolicy.deny_all()
        )
        try:
            remote, _ = await vercel_sandbox.get_or_create_sandbox(
                name=name,
                persistent=True,
                env={},
                tags={OWNER_TAG: self.owner},
                execution_time_limit=self.execution_time_limit,
                resources=vercel_sandbox.SandboxResources(vcpus=self.cpus, memory=self.memory),
                network_policy=network_policy,
                snapshot_retention=vercel_sandbox.SnapshotRetention(count=1, delete_evicted=True),
            )
        except vercel_sandbox.SandboxApiError as error:
            if error.status_code != 409:
                raise
            # The SDK does lookup-then-create; the service's unique name picks a winner.
            remote = await vercel_sandbox.get_sandbox(name=name)
        self._check_owner(remote, name)
        try:
            await remote.update_network_policy(network_policy)
        except Exception:
            await remote.stop()
            raise
        return remote

    async def release(self, sandbox: provider.Sandbox, *, keep: bool) -> None:
        if not isinstance(sandbox, VercelSandbox) or sandbox.provider is not self:
            raise ValueError("sandbox does not belong to this provider")
        same = True
        try:
            current = await vercel_sandbox.get_sandbox(name=sandbox.name)
            self._check_owner(current, sandbox.name)
            same = current.created_at == sandbox.remote.created_at
            if same and keep:
                await sandbox.remote.stop()  # native persistence keeps the filesystem
            elif same:
                await current.destroy()
        except vercel_sandbox.SandboxApiError as error:
            if error.status_code != 404:
                raise
        sandbox.active = False
        record = await store.get(sandbox.name.removeprefix("hatchery-"))
        if sandbox.purpose != "thread" or not same or record is None:
            return
        if keep:
            record.status = "stopped"
            record.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
            await store.save(record)
        else:
            await store.delete(record.id)

    async def destroy(self, name: str) -> None:
        try:
            remote = await self._existing(name)
        except FileNotFoundError:
            remote = None
        if remote is not None:
            await remote.destroy()
        record = await store.get(name.removeprefix("hatchery-"))
        if record is not None and record.sandbox_name == name:
            await store.delete(record.id)
