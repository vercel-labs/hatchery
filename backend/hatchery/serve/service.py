"""Resolve `main`, run agent handlers and jobs in the agent's serve sandbox, apply effects.

Ported from agentmesh `serve/service.py`. Only published `main` code is served: each
request reads the agent's directory at the current `main` revision (cached briefly,
`serve.revision_ttl_seconds`) and redeploys the one serve sandbox when the revision
moved, so a merged route goes live without a Hatchery redeploy. Thread branches are
never served. An in-sandbox `flock` serializes deploys, requests, and jobs, so each
agent runs one request at a time (busy: 429). Handlers run with `/workspace/data` as
working directory, `HOME`, and `HATCHERY_DATA`, and receive only that agent's secrets.
`prompt()` effects land as normal turns of the same agent (`supervisor.prompt`).
"""

import asyncio
import base64
import dataclasses
import json
import logging
import os
import re
import shlex
import typing
from typing import Any

import rotor
import rotor.patterns

from hatchery import environment, runtime, vault
from hatchery.agent import supervisor
from hatchery.serve import routing, scheduling
from hatchery.worker import provider
from hatchery.workspace import files

MAX_EFFECTS = 4
BUSY_EXIT = 75
STALE_EXIT = 76
DATA = f"{provider.WORKSPACE}/data"
REVISION_FILE = ".hatchery/serve-revision"
_RESPONSE_FIELDS = {"version", "status", "headers", "body_b64", "effects"}
_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Deployment:
    revision: str
    tree: files.Tree
    routes: routing.RouteTable
    jobs: tuple[scheduling.Job, ...]
    expires_at: float


@dataclasses.dataclass(frozen=True)
class HandlerResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclasses.dataclass(frozen=True)
class _Active:
    sandbox: provider.Sandbox
    revision: str
    runtime: str


def invocation_command(revision: str, runtime_path: str) -> str:
    """Run the SDK only if the sandbox still holds `revision`; exit 75 when busy."""
    runner = (
        f'test "$(cat {provider.WORKSPACE}/{REVISION_FILE} 2>/dev/null)" = "$1" '
        '|| exit 76; cd "$2"; export PYTHONPATH=/workspace/self; exec python3 -m hatchery.sdk'
    )
    return (
        f"flock -n -E 75 {provider.WORKSPACE}/.hatchery/serve.lock sh -c "
        f"{shlex.quote(runner)} hatchery {shlex.quote(revision)} {shlex.quote(runtime_path)}"
    )


class ServeService:
    """The worker-side coordinator for public agent routes and scheduled jobs."""

    def __init__(self, env: environment.Environment) -> None:
        self.env = env
        self._deployments: dict[str, Deployment] = {}
        self._retired: set[str] = set()
        self._sandboxes: dict[str, _Active] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def sandbox_name(self, agent_id: str) -> str:
        """One persistent serve sandbox per agent and deployment environment."""
        kind = os.environ.get("VERCEL_ENV", "local")
        if kind == "preview":
            branch = os.environ.get("VERCEL_BRANCH_URL") or os.environ.get(
                "VERCEL_GIT_COMMIT_REF", ""
            )
            kind = f"preview:{branch}"
        return provider.sandbox_name(f"serve:v1:{kind}:{agent_id}")

    def url(self, agent_id: str, port: int | None = None) -> str:
        domain = self.env.config.serve.domain
        if domain == "localhost":
            return f"http://{agent_id}.localhost{f':{port}' if port else ''}"
        return f"https://{agent_id}.{domain}"

    def main_advanced(self, revision: str) -> None:
        """Drop cached deployments after another `main` revision was observed."""
        self._deployments.clear()

    async def deployment(self, agent_id: str, *, refresh: bool = False) -> Deployment:
        cached = self._deployments.get(agent_id)
        if not refresh and cached is not None and cached.expires_at >= self.env.now():
            return cached
        revision, tree = await self.env.workspaces.read_owner_main(agent_id)
        deployment = Deployment(
            revision,
            tree,
            routing.discover_routes(tree),
            scheduling.discover_jobs(tree),
            self.env.now() + self.env.config.serve.revision_ttl_seconds,
        )
        self._deployments[agent_id] = deployment
        return deployment

    async def status(self, agent_id: str, *, port: int | None = None) -> dict[str, Any]:
        """Main revision, the revision the serve sandbox holds, and retirement."""
        deployment = await self.deployment(agent_id)
        name = self.sandbox_name(agent_id)
        try:
            preview = await self.env.sandboxes.read_file(name, REVISION_FILE, limit=41)
            deployed: str | None = preview.content.decode(errors="ignore").strip() or None
        except FileNotFoundError:
            deployed = None
        return {
            "agent_id": agent_id,
            "url": self.url(agent_id, port),
            "revision": deployment.revision,
            "deployed_revision": deployed,
            "sandbox": name,
            "retired": agent_id in self._retired or await vault.retired(agent_id),
            "routes": sum(route.available for route in deployment.routes.routes),
            "schedules": sum(job.available and job.enabled for job in deployment.jobs),
        }

    async def list_routes(self, agent_id: str, *, port: int | None = None) -> dict[str, Any]:
        deployment = await self.deployment(agent_id)
        base = self.url(agent_id, port)
        return {
            "revision": deployment.revision,
            "routes": [
                {
                    "path": route.path,
                    "methods": list(route.methods),
                    "url": base + route.path,
                    "description": route.description,
                    "available": route.available,
                    "error": route.error,
                }
                for route in deployment.routes.routes
            ],
        }

    async def list_schedules(self, agent_id: str) -> dict[str, Any]:
        from hatchery.agent import runtime as agent_runtime

        client = agent_runtime.client
        deployment = await self.deployment(agent_id)
        durable: dict[str, Any] = {"revision": "", "jobs": {}, "history": [], "running": []}
        try:
            durable, _ = await client.query(
                scheduling.scheduler_id(agent_id), scheduling.Scheduler.status
            )
        except rotor.ProcessNotFound:
            pass
        jobs = durable["jobs"]
        next_at: dict[str, float | None] = {}
        for name, status in jobs.items():
            if status.get("ticker") is None:
                continue
            try:
                ticker, _ = await client.read_state(status["ticker"], rotor.patterns.Ticker)
            except rotor.ProcessNotFound:
                continue
            next_at[name] = ticker.next_at
        return {
            "revision": deployment.revision,
            "reconciled_revision": durable["revision"],
            "schedules": [
                {
                    "name": job.name,
                    "description": job.description,
                    "kind": job.kind,
                    "value": job.value,
                    "timezone": job.timezone,
                    "enabled": job.enabled,
                    "paused": bool(jobs.get(job.name, {}).get("paused")),
                    "available": job.available,
                    "error": job.error,
                    "next_at": next_at.get(job.name),
                    "running": any(item.get("name") == job.name for item in durable["running"]),
                    "last_run": next(
                        (i for i in reversed(durable["history"]) if i.get("name") == job.name),
                        None,
                    ),
                }
                for job in deployment.jobs
            ],
        }

    async def invoke(
        self,
        agent_id: str,
        *,
        request_id: str,
        method: str,
        path: str,
        query: list[tuple[str, str]],
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> HandlerResponse:
        if agent_id in self._retired or await vault.retired(agent_id):
            return HandlerResponse(410)
        deployment = await self.deployment(agent_id)
        while True:
            resolution = deployment.routes.resolve(path, method)
            if resolution.status_code != 200:
                allowed = (
                    (("Allow", ", ".join(resolution.allowed_methods)),)
                    if resolution.status_code == 405
                    else ()
                )
                return HandlerResponse(resolution.status_code, allowed)
            assert resolution.route is not None
            sandbox, runtime_path = await self._prepare_sandbox(agent_id, deployment)
            envelope = {
                "version": 1,
                "kind": "request",
                "request_id": request_id,
                "handler": f"{provider.WORKSPACE}/{resolution.route.handler_path}",
                "method": method,
                "path": path,
                "params": resolution.params,
                "query": query,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode(),
            }
            try:
                result = await sandbox.exec(
                    invocation_command(deployment.revision, runtime_path),
                    timeout=self.env.config.serve.request_timeout_seconds,
                    env={"HATCHERY_DATA": DATA, "HOME": DATA, **await vault.values(agent_id)},
                    stdin=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(),
                )
            except Exception:
                self._sandboxes.pop(agent_id, None)
                raise
            if result.exit_code != STALE_EXIT:
                break
            # Another worker deployed a newer main in the meantime: follow it once.
            latest = await self.deployment(agent_id, refresh=True)
            if latest.revision == deployment.revision:
                raise RuntimeError("serve sandbox revision did not settle")
            self._sandboxes.pop(agent_id, None)
            deployment = latest
        if result.exit_code == BUSY_EXIT:
            return HandlerResponse(429, (("Retry-After", "1"),))
        if result.timed_out:
            log.warning("serve handler timed out agent=%s revision=%s", agent_id, deployment.revision)
            return HandlerResponse(504)
        if result.exit_code:
            log.warning(
                "serve handler failed agent=%s revision=%s exit=%s",
                agent_id,
                deployment.revision,
                result.exit_code,
            )
            return HandlerResponse(502)
        response, effects = _response(result)
        await self._apply_effects(agent_id, effects, source="api")
        return response

    async def invoke_job(
        self,
        agent_id: str,
        *,
        revision: str,
        name: str,
        handler_path: str,
        scheduled_for: float,
        run_id: str,
    ) -> dict[str, Any]:
        """Invoke one discovered job at the revision that armed its occurrence."""
        if agent_id in self._retired or await vault.retired(agent_id):
            return {"status": 410, "result": None, "error": "agent is retired"}
        cached = self._deployments.get(agent_id)
        if cached is not None and cached.revision == revision:
            deployment = cached
        else:
            tree = await self.env.workspaces.read_owner_at(agent_id, revision)
            deployment = Deployment(
                revision,
                tree,
                routing.discover_routes(tree),
                scheduling.discover_jobs(tree),
                self.env.now() + self.env.config.serve.revision_ttl_seconds,
            )
        if not any(
            job.name == name and job.handler_path == handler_path and job.available and job.enabled
            for job in deployment.jobs
        ):
            return {"status": 410, "result": None, "error": "schedule is no longer available"}
        sandbox, runtime_path = await self._prepare_sandbox(agent_id, deployment)
        envelope = {
            "version": 1,
            "kind": "job",
            "run_id": run_id,
            "handler": f"{provider.WORKSPACE}/{handler_path}",
            "name": name,
            "scheduled_for": scheduled_for,
            "revision": revision,
        }
        result = await sandbox.exec(
            invocation_command(revision, runtime_path),
            timeout=self.env.config.serve.job_timeout_seconds,
            env={"HATCHERY_DATA": DATA, "HOME": DATA, **await vault.values(agent_id)},
            stdin=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(),
        )
        if result.exit_code == BUSY_EXIT:
            raise RuntimeError("agent serve sandbox is busy")
        if result.exit_code == STALE_EXIT:
            self._sandboxes.pop(agent_id, None)
            raise RuntimeError("agent serve sandbox revision changed")
        if result.timed_out:
            return {"status": 504, "result": None, "error": "job timed out"}
        if result.exit_code:
            return {
                "status": 502,
                "result": None,
                "error": (result.stderr or "job process failed")[-2000:],
            }
        response, effects = _response(result)
        if agent_id in self._retired or await vault.retired(agent_id):
            return {"status": 410, "result": None, "error": "agent is retired"}
        await self._apply_effects(agent_id, effects, source="schedule")
        value: Any = None
        if response.body:
            try:
                value = json.loads(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = response.body.decode(errors="replace")[:10000]
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) > 10_000:
            value = f"[result omitted: {len(encoded)} characters]"
        return {
            "status": response.status,
            "result": value,
            "error": result.stderr[-2000:] if response.status >= 500 else "",
        }

    async def retire(self, agent_id: str, request_id: str) -> None:
        """Clear the vault (tombstone), destroy the serve sandbox and its data; later 410."""
        self._retired.add(agent_id)
        self._deployments.pop(agent_id, None)
        self._sandboxes.pop(agent_id, None)
        await vault.retire(agent_id, request_id)
        await self.env.sandboxes.destroy(self.sandbox_name(agent_id))

    async def _prepare_sandbox(
        self, agent_id: str, deployment: Deployment
    ) -> tuple[provider.Sandbox, str]:
        async with self._locks.setdefault(agent_id, asyncio.Lock()):
            active = self._sandboxes.get(agent_id)
            if active is not None and active.revision == deployment.revision:
                return active.sandbox, active.runtime
            name = self.sandbox_name(agent_id)

            async def seed() -> files.Tree:
                return deployment.tree

            acquired = await self.env.sandboxes.acquire(
                name, seed=seed, owner=agent_id, purpose="serve"
            )
            sandbox = acquired.sandbox
            deployed = None
            if not acquired.fresh:
                try:
                    preview = await self.env.sandboxes.read_file(name, REVISION_FILE, limit=41)
                    if not preview.truncated:
                        deployed = preview.content.decode(errors="ignore").strip()
                except FileNotFoundError:
                    pass
            if deployed != deployment.revision:
                await sandbox.deploy(deployment.tree, deployment.revision)
            runtime_path = await sandbox.install_runtime(runtime.sandbox_files())
            self._sandboxes[agent_id] = _Active(sandbox, deployment.revision, runtime_path)
            return sandbox, runtime_path

    async def _apply_effects(
        self, agent_id: str, effects: list[dict[str, Any]], *, source: str
    ) -> None:
        if len(effects) > MAX_EFFECTS:
            raise ValueError("handler returned too many effects")
        for effect in effects:
            if set(effect) not in ({"type", "text", "key"}, {"type", "text", "key", "thread"}):
                raise ValueError("invalid prompt effect")
            if effect["type"] != "prompt" or not all(
                isinstance(effect[field], str) and effect[field].strip() for field in ("text", "key")
            ):
                raise ValueError("invalid prompt effect")
            thread = effect.get("thread")
            if (
                len(effect["text"]) > 100_000
                or len(effect["key"]) > 200
                or (thread is not None and (not isinstance(thread, str) or len(thread) > 200))
            ):
                raise ValueError("prompt effect exceeds limit")
        for effect in effects:
            thread = effect.get("thread")
            await supervisor.prompt(
                agent_id,
                effect["text"],
                key=effect["key"],
                thread=thread if isinstance(thread, str) and thread.strip() else None,
                source=typing.cast(typing.Literal["api", "schedule"], source),
            )


def _response(result: provider.ExecResult) -> tuple[HandlerResponse, list[dict[str, Any]]]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("response JSON contains duplicate fields")
            value[key] = item
        return value

    value = json.loads(result.stdout, object_pairs_hook=unique)
    if type(value) is not dict or set(value) != _RESPONSE_FIELDS or value.get("version") != 1:
        raise ValueError("invalid handler response envelope")
    status = value.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
        raise ValueError("invalid handler response status")
    raw_headers = value.get("headers")
    if not isinstance(raw_headers, list):
        raise ValueError("invalid handler response headers")
    headers = []
    for pair in raw_headers:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", pair[0])
            or "\r" in pair[1]
            or "\n" in pair[1]
        ):
            raise ValueError("invalid handler response header")
        try:
            pair[1].encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError("handler response header must be Latin-1") from error
        if pair[0].lower() not in _HOP_HEADERS:
            headers.append((pair[0], pair[1]))
    encoded = value.get("body_b64")
    if not isinstance(encoded, str):
        raise ValueError("invalid handler response body")
    body = base64.b64decode(encoded, validate=True)
    if base64.b64encode(body).decode() != encoded:
        raise ValueError("handler response body is not canonical base64")
    if len(body) > 1024 * 1024:
        raise ValueError("handler response body exceeds limit")
    effects = value.get("effects")
    if not isinstance(effects, list) or any(type(effect) is not dict for effect in effects):
        raise ValueError("invalid handler effects")
    return HandlerResponse(status, tuple(headers), body), effects


def current() -> ServeService:
    """The serve service of the installed Environment (process-local caches)."""
    env = environment.Environment.current()
    if env.services.serve is None:
        env.services.serve = ServeService(env)
    return typing.cast(ServeService, env.services.serve)
