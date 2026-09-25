"""The worker's environment: config, workspace repo, review, sandboxes, model, clock.

Ported from agentmesh `mesh.py` (`Mesh`). Rotor constructs processes with no
arguments, so handlers reach their dependencies through `Environment.current()`.
Exactly one Environment is installed per worker process; tests swap it with `use()`.
None of this is ever part of a Rotor checkpoint. Importing this module does no I/O.
"""

import collections.abc
import contextlib
import dataclasses
import os
import time

import ai

from hatchery import config, model_budget
from hatchery.worker import provider
from hatchery.worker import sandbox as worker_sandbox
from hatchery.workspace import connect as workspace_connect
from hatchery.workspace import git as workspace_git
from hatchery.workspace import repo as workspace_repo
from hatchery.workspace import review as workspace_review

_current: Environment | None = None


@dataclasses.dataclass
class Services:
    """Process-local services available to zero-argument Rotor process instances.

    `serve.service.current()` and `serve.scheduling.current()` create them lazily, so
    every Environment (and every test) gets its own caches.
    """

    serve: object | None = None
    schedules: object | None = None


@dataclasses.dataclass(frozen=True)
class Environment:
    config: config.Config
    workspaces: workspace_repo.WorkspaceRepo
    review: workspace_review.Review
    sandboxes: provider.SandboxProvider
    model: ai.Model
    clock: collections.abc.Callable[[], float] = time.time
    model_limits: model_budget.ModelLimits | None = None
    secrets_key: str | None = None
    services: Services = dataclasses.field(default_factory=Services, compare=False)

    def __post_init__(self) -> None:
        limits = self.model_limits or model_budget.resolve_limits(
            self.config.model.id,
            context_override=self.config.model.context_window_tokens,
        )
        limits.input_limit(self.config.model.max_output_tokens)
        object.__setattr__(self, "model_limits", limits)

    def now(self) -> float:
        return self.clock()

    @staticmethod
    def current() -> Environment:
        if _current is None:
            raise RuntimeError("no Environment installed; wrap the worker in `with env.use():`")
        return _current

    def install(self) -> None:
        """Install this Environment for the rest of the process.

        Hosted functions have no lifespan to scope `use()` to: the queue subscribers
        that run agent code are invoked by the platform, outside any ASGI app.
        """
        global _current
        _current = self

    @contextlib.contextmanager
    def use(self) -> collections.abc.Iterator[Environment]:
        """Install this Environment for the duration of the block."""
        global _current
        previous, _current = _current, self
        try:
            yield self
        finally:
            _current = previous

    def with_clock(self, clock: collections.abc.Callable[[], float]) -> Environment:
        return dataclasses.replace(self, clock=clock)

    @classmethod
    def from_env(cls, *, sandboxes: provider.SandboxProvider | None = None) -> Environment:
        """The deployment from `HATCHERY_*` variables, without I/O.

        `HATCHERY_STORAGE_REPO` names the storage repository. A `file://` remote
        reviews proposals locally; anything else is GitHub, authenticated by the
        Connect GitHub App or `GITHUB_TOKEN`.
        """
        settings = config.load()
        remote = workspace_connect.storage_remote()
        workspaces = workspace_repo.WorkspaceRepo(
            remote, token=workspace_connect.github_credentials(remote)
        )
        review: workspace_review.Review = (
            workspace_review.LocalReview(workspaces)
            if workspace_git.parse_remote(remote)[0] == "file"
            else workspace_review.GitHubReview(workspaces)
        )
        return cls(
            config=settings,
            workspaces=workspaces,
            review=review,
            sandboxes=sandboxes or worker_sandbox.VercelSandboxProvider(),
            model=ai.get_model(settings.model.id),
            secrets_key=os.getenv("HATCHERY_SECRETS_KEY"),
        )
