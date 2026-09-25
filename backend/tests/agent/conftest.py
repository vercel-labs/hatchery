"""A running agent: Rotor LocalRuntime, real local Git, a scripted model and sandbox."""

import contextlib
import dataclasses
import pathlib
import typing

import ai
import ai.testing
import pytest
import rotor.testing

from hatchery import config, environment, model_budget
from hatchery.agent import runtime, supervisor, thread
from hatchery.store import agents, chats, events
from hatchery.worker import provider, scripted
from hatchery.workspace import files as workspace_files
from hatchery.workspace import local as workspace_local
from hatchery.workspace import repo as workspace_repo
from hatchery.workspace import review as workspace_review

CONFIG = config.Config(
    thread=config.ThreadConfig(max_turns=4, bash_calls_per_turn=2, command_timeout_seconds=7),
    budget=config.BudgetConfig(tokens_per_day=1000),
)
AGENT = "hatchery"


@pytest.fixture
async def repo(
    tmp_path: pathlib.Path, request: pytest.FixtureRequest
) -> workspace_repo.WorkspaceRepo:
    """Storage with the agent and a wiki; parametrize with `False` for no `wiki/`."""
    root = tmp_path / "workspace"
    agent = root / "agents" / AGENT
    agent.mkdir(parents=True)
    (agent / "AGENTS.md").write_text("Be precise and preserve memory.\n")
    (agent / "USER.md").write_text("# Team\n")
    (agent / "MEMORY.md").write_text("# Core memory\n")
    skill = agent / "skills" / "release"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release\ndescription: Cut and verify a release.\n---\n# Release\n"
    )
    (skill / "references" / "checks.md").write_text("# Verification checks\n")
    if getattr(request, "param", True):
        (root / "wiki").mkdir()
        (root / "wiki" / "guide.md").write_text("Original shared guide.\n")
    await agents.default()
    return workspace_repo.WorkspaceRepo(await workspace_local.initialize_local(root))


def edit(path: str, content: str, stdout: str = "edited") -> scripted.ScriptedCommand:
    return scripted.ScriptedCommand(
        provider.ExecResult(0, stdout), writes={path: workspace_files.File(content.encode())}
    )


@dataclasses.dataclass
class Running:
    rt: rotor.testing.LocalRuntime
    repo: workspace_repo.WorkspaceRepo
    model: ai.testing.FakeModel
    sandboxes: scripted.ScriptedSandboxProvider

    async def chat(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        drain: bool = True,
        origin: typing.Literal["ui", "channel", "worker", "cron"] = "ui",
        turn_id: str | None = None,
    ) -> str:
        """Post one message the way the server does, then run the runtime to rest."""
        if chat_id is None:
            chat_id = (await chats.create(AGENT, "test", user_id="user_test")).id
        message = ai.user_message(text)
        await events.append(chat_id, "messages", message.model_dump(mode="json"))
        await supervisor.start_turn(chat_id, origin, turn_id=turn_id, actor_user_id="user_test")
        if drain:
            await self.rt.drain()
        return chat_id

    async def roster(self) -> dict[str, typing.Any]:
        return await supervisor.roster(AGENT)

    async def budget(self) -> dict[str, typing.Any]:
        return (await self.roster())["budget"]

    async def thread_id(self, chat_id: str) -> str:
        thread_id = await supervisor.thread_for_chat(chat_id)
        assert thread_id is not None
        return thread_id

    async def details(self, chat_id: str) -> dict[str, typing.Any]:
        value, _ = await self.rt.client.query(
            await self.thread_id(chat_id), thread.AgentThread.details
        )
        return typing.cast(dict[str, typing.Any], value)

    async def send(self, chat_id: str, msg: typing.Any) -> None:
        await self.rt.client.send(await self.thread_id(chat_id), msg)
        await self.rt.drain()

    def sandbox(self, details: dict[str, typing.Any]) -> scripted.ScriptedSandbox:
        return self.sandboxes.sandboxes[details["sandbox"]]

    def tool_results(self, details: dict[str, typing.Any]) -> list[typing.Any]:
        return [
            part.result
            for message in map(ai.messages.Message.model_validate, details["messages"])
            for part in message.tool_results
        ]


type Run = typing.Callable[..., contextlib.AbstractAsyncContextManager[Running]]


@pytest.fixture
def run(repo: workspace_repo.WorkspaceRepo, monkeypatch: pytest.MonkeyPatch) -> Run:
    """`async with run(*scripts, commands=..., settings=...) as app:`"""

    @contextlib.asynccontextmanager
    async def running(
        *scripts: typing.Sequence[ai.messages.Message],
        commands: typing.Mapping[str, provider.ExecResult | scripted.ScriptedCommand]
        | None = None,
        sandboxes: scripted.ScriptedSandboxProvider | None = None,
        settings: config.Config = CONFIG,
        model_limits: model_budget.ModelLimits | None = None,
        strict: bool = True,
    ) -> typing.AsyncIterator[Running]:
        model = ai.testing.FakeModel(*scripts)
        sandboxes = sandboxes or scripted.ScriptedSandboxProvider(commands)
        async with rotor.testing.LocalRuntime(*supervisor.PROCESSES, strict=strict) as rt:
            monkeypatch.setattr(runtime, "client", rt.client)
            env = environment.Environment(
                settings,
                repo,
                workspace_review.LocalReview(repo),
                sandboxes,
                model,
                rt.clock.now,
                model_limits=model_limits,
            )
            with env.use():
                yield Running(rt, repo, model, sandboxes)

    return running
