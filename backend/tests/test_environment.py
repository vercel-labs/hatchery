import pytest

from hatchery import config, environment, model_budget
from hatchery.worker import sandbox, scripted
from hatchery.workspace import review


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "_current", None)
    for name in ("GITHUB_CONNECTOR", "GITHUB_TOKEN", "HATCHERY_SECRETS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HATCHERY_STORAGE_REPO", (tmp_path / "storage.git").as_uri())


def test_from_env_builds_a_local_deployment_without_io(monkeypatch, tmp_path):
    monkeypatch.setenv("HATCHERY_SECRETS_KEY", "key")
    monkeypatch.setenv("HATCHERY_THREAD_MAX_TURNS", "7")

    env = environment.Environment.from_env()

    assert env.workspaces.remote == (tmp_path / "storage.git").as_uri()
    assert isinstance(env.review, review.LocalReview)
    assert isinstance(env.sandboxes, sandbox.VercelSandboxProvider)
    assert env.config.thread.max_turns == 7
    assert env.secrets_key == "key"
    assert env.model_limits == model_budget.resolve_limits(config.ModelConfig().id)
    assert not (tmp_path / "storage.git").exists()


def test_from_env_reviews_github_storage_on_github(monkeypatch):
    monkeypatch.setenv("HATCHERY_STORAGE_REPO", "vercel-internal-playground/hatchery-storage")
    monkeypatch.setenv("GITHUB_TOKEN", "static-token")
    sandboxes = scripted.ScriptedSandboxProvider()

    env = environment.Environment.from_env(sandboxes=sandboxes)

    assert env.workspaces.remote == (
        "https://github.com/vercel-internal-playground/hatchery-storage.git"
    )
    assert env.workspaces.has_credentials
    assert isinstance(env.review, review.GitHubReview)
    assert env.sandboxes is sandboxes


def test_current_requires_an_installed_environment_and_use_restores_the_previous_one():
    first = environment.Environment.from_env(sandboxes=scripted.ScriptedSandboxProvider())
    second = environment.Environment.from_env(sandboxes=scripted.ScriptedSandboxProvider())
    with pytest.raises(RuntimeError, match="no Environment installed"):
        environment.Environment.current()

    first.install()
    with second.use() as active:
        assert active is second and environment.Environment.current() is second
    assert environment.Environment.current() is first


def test_with_clock_replaces_only_the_clock():
    env = environment.Environment.from_env(sandboxes=scripted.ScriptedSandboxProvider())

    fixed = env.with_clock(lambda: 1234.0)

    assert fixed.now() == 1234.0
    assert fixed.workspaces is env.workspaces and fixed.services is env.services
    assert env.now() != 1234.0


def test_model_limits_come_from_config_and_reject_an_impossible_output_reserve(monkeypatch):
    monkeypatch.setenv("HATCHERY_MODEL_ID", "custom/model-without-metadata")
    with pytest.raises(ValueError, match="HATCHERY_MODEL_CONTEXT_WINDOW_TOKENS"):
        environment.Environment.from_env(sandboxes=scripted.ScriptedSandboxProvider())

    monkeypatch.setenv("HATCHERY_MODEL_CONTEXT_WINDOW_TOKENS", "32000")
    env = environment.Environment.from_env(sandboxes=scripted.ScriptedSandboxProvider())
    assert env.model_limits == model_budget.ModelLimits(32_000, 32_000)

    monkeypatch.delenv("HATCHERY_MODEL_ID")
    monkeypatch.delenv("HATCHERY_MODEL_CONTEXT_WINDOW_TOKENS")
    monkeypatch.setenv("HATCHERY_MODEL_MAX_OUTPUT_TOKENS", "500000")
    with pytest.raises(ValueError, match="provider limit"):
        environment.Environment.from_env(sandboxes=scripted.ScriptedSandboxProvider())
