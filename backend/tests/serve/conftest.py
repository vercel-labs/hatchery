"""A running agent with serving: LocalRuntime with every process, real local Git, a
scripted model, and the scripted sandbox provider. Serve sandboxes run the real SDK in a
subprocess against the files deployed to them, so handlers execute for real."""

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import shlex
import sys
import typing

import ai
import ai.testing
import httpx
import pytest
import rotor.testing

from hatchery import config, environment
from hatchery.agent import runtime
from hatchery.app import server
from hatchery.serve import scheduling
from hatchery.worker import provider, scripted
from hatchery.workspace import repo as workspace_repo
from hatchery.workspace import review as workspace_review

from tests.agent import conftest as agent_conftest
from tests.agent.conftest import repo  # noqa: F401  (fixture)

AGENT = agent_conftest.AGENT
KEY = base64.urlsafe_b64encode(b"g" * 32).rstrip(b"=").decode()
BACKEND = pathlib.Path(__file__).resolve().parents[2]


class LocalServeSandbox(scripted.ScriptedSandbox):
    """Runs `serve.service.invocation_command` as the SDK in a real subprocess."""

    def __init__(self, name: str, script: scripted.Script, root: pathlib.Path) -> None:
        super().__init__(name, script)
        self.root = root
        self.busy = False

    async def exec(self, command, *, timeout, env=None, stdin=None) -> provider.ExecResult:
        if "-m hatchery.sdk" not in command:
            return await super().exec(command, timeout=timeout, env=env, stdin=stdin)
        self.commands.append(command)
        self.inputs.append(stdin)
        self.environments.append(dict(env or {}))
        if self.busy:
            return provider.ExecResult(75)  # flock -n -E 75
        revision = shlex.split(command)[-2]
        if self.inspection_files.get(".hatchery/serve-revision", b"").decode() != revision:
            return provider.ExecResult(76)
        code = self.root / "self"
        data = self.root / "data"
        data.mkdir(parents=True, exist_ok=True)
        for path, file in self.files.items():
            if path.startswith("self/"):
                (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                (self.root / path).write_bytes(file.content)
        assert stdin is not None
        envelope = json.loads(stdin)
        envelope["handler"] = envelope["handler"].replace(provider.WORKSPACE, str(self.root), 1)
        variables = {**(env or {}), "HATCHERY_DATA": str(data), "HOME": str(data)}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "hatchery.sdk",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join([str(code), str(BACKEND)]),
                **variables,
            },
        )
        stdout, stderr = await process.communicate(json.dumps(envelope).encode())
        return provider.ExecResult(process.returncode or 0, stdout.decode(), stderr.decode())


class LocalServeProvider(scripted.ScriptedSandboxProvider):
    def __init__(self, root: pathlib.Path, script: scripted.Script | None = None) -> None:
        super().__init__(script)
        self.root = root

    async def acquire(self, name, *, seed, owner="", purpose="thread", chat=None):
        if purpose != "serve" or name in self.sandboxes:
            return await super().acquire(name, seed=seed, owner=owner, purpose=purpose, chat=chat)
        sandbox = self.sandboxes[name] = LocalServeSandbox(name, self.script, self.root / name)
        await sandbox.upload(await seed())
        sandbox.inspection_files["data/.keep"] = b""
        self.seeds += 1
        return provider.Acquired(sandbox, fresh=True)

    def serve_sandboxes(self) -> list[LocalServeSandbox]:
        return [s for s in self.sandboxes.values() if isinstance(s, LocalServeSandbox)]


class Serving(agent_conftest.Running):
    sandboxes: LocalServeProvider  # type: ignore[assignment]
    app: httpx.AsyncClient

    def public(self, agent_id: str = AGENT) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url=f"http://{agent_id}.localhost")

    async def merge_tree(self, agent_id: str, changes: dict[str, typing.Any], name: str) -> str:
        """Publish a change to `main` the way a thread does: checkpoint, propose, merge."""
        branch = self.repo.thread_branch(agent_id, name)
        tree = {
            path: file
            for path, file in (await self.repo.materialize(agent_id, branch)).items()
            if path.startswith(("self/", "wiki/"))
        }
        for path, file in changes.items():
            if file is None:
                tree.pop(path, None)
            else:
                tree[path] = file
        head = await self.repo.checkpoint(
            agent_id, branch, tree, process_id=name, operation_id=f"{name}:checkpoint"
        )
        outcome = await self.repo.consolidate(
            agent_id, branch, "workspace", head=head, process_id=name, summary=name
        )
        assert outcome.proposal
        return await self.repo.merge(outcome.proposal)


type Serve = typing.Callable[..., contextlib.AbstractAsyncContextManager[Serving]]


@pytest.fixture
def serve(
    repo: workspace_repo.WorkspaceRepo,  # noqa: F811
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Serve:
    """`async with serve(*scripts, commands=...) as app:`"""

    @contextlib.asynccontextmanager
    async def running(
        *scripts: typing.Sequence[ai.messages.Message],
        commands: typing.Mapping[str, provider.ExecResult | scripted.ScriptedCommand]
        | None = None,
        settings: config.Config = agent_conftest.CONFIG,
    ) -> typing.AsyncIterator[Serving]:
        model = ai.testing.FakeModel(*scripts)
        sandboxes = LocalServeProvider(tmp_path / "sandboxes", commands)
        async with rotor.testing.LocalRuntime(*runtime.PROCESSES, strict=True) as rt:
            monkeypatch.setattr(runtime, "client", rt.client)
            env = environment.Environment(
                settings,
                repo,
                workspace_review.LocalReview(repo),
                sandboxes,
                model,
                rt.clock.now,
                secrets_key=KEY,
            )
            repo.set_main_observer(scheduling.observe)
            transport = httpx.ASGITransport(app=server.app)
            with env.use():
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as app:
                    running = Serving(rt, repo, model, sandboxes)
                    running.app = app
                    yield running
            repo.set_main_observer(None)

    return running

