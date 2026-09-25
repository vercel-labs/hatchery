import pytest

from hatchery import config


@pytest.mark.parametrize(
    ("environ", "match"),
    [
        ({"HATCHERY_BUDGET_TOKENS_PER_DAY": "0"}, "HATCHERY_BUDGET_TOKENS_PER_DAY"),
        ({"HATCHERY_THREAD_MAX_TURNS": "nine"}, "HATCHERY_THREAD_MAX_TURNS"),
        ({"HATCHERY_REVIEW_WIKI": "maybe"}, "HATCHERY_REVIEW_WIKI"),
        (
            {"HATCHERY_THREAD_COMMAND_TIMEOUT_SECONDS": "481"},
            "HATCHERY_THREAD_COMMAND_TIMEOUT_SECONDS",
        ),
        ({"HATCHERY_MODEL_ID": "gpt-5"}, "provider/model"),
        (
            {
                "HATCHERY_MODEL_MAX_OUTPUT_TOKENS": "1000",
                "HATCHERY_MODEL_CONTEXT_WINDOW_TOKENS": "1000",
            },
            "smaller than context_window_tokens",
        ),
        ({"HATCHERY_SERVE_DOMAIN": "example.com:8080"}, "HATCHERY_SERVE_DOMAIN"),
    ],
    ids=[
        "zero-budget",
        "non-integer",
        "policy",
        "command-timeout",
        "model-id",
        "output-exhausts-context",
        "domain-port",
    ],
)
def test_invalid_configuration_names_the_variable(environ, match):
    with pytest.raises(config.ConfigError, match=match):
        config.load(environ)


def test_defaults_apply_and_explicit_values_are_kept():
    loaded = config.load(
        {
            "HATCHERY_MODEL_ID": "anthropic/claude-4",
            "HATCHERY_MODEL_CONTEXT_WINDOW_TOKENS": "200000",
            "HATCHERY_THREAD_MAX_TURNS": "7",
            "HATCHERY_THREAD_COMPACT_ABOVE_TOKENS": "50000",
            "HATCHERY_REVIEW_WORKSPACE": "review",
            "HATCHERY_SERVE_DOMAIN": "Agents.Example.COM.",
            "UNRELATED": "ignored",
        }
    )
    assert loaded.model.id == "anthropic/claude-4"
    assert loaded.model.max_output_tokens == 4096
    assert loaded.model.context_window_tokens == 200_000
    assert (loaded.thread.max_turns, loaded.thread.compact_above_tokens) == (7, 50000)
    assert loaded.thread.bash_calls_per_turn == 16
    assert loaded.thread.command_timeout_seconds == 300
    assert loaded.thread.sandbox_idle_seconds == 300
    assert (loaded.thread.max_delegation_depth, loaded.thread.max_delegations_per_thread) == (2, 4)
    assert (loaded.review.workspace, loaded.review.serve, loaded.review.wiki) == (
        "review",
        "review",
        "review",
    )
    assert loaded.budget.tokens_per_day == 1_000_000
    assert loaded.serve.domain == "agents.example.com"
    assert loaded.serve.request_timeout_seconds == 30


def test_empty_environment_uses_agentmesh_defaults_and_hatchery_model():
    loaded = config.load({})
    assert loaded == config.Config()
    assert loaded.model.id == "openai/gpt-5.6-sol"
    assert (loaded.review.workspace, loaded.review.serve, loaded.review.wiki) == (
        "auto",
        "review",
        "review",
    )


@pytest.mark.parametrize("value", ["hatchery", "a", "agent-2", "a" * 63])
def test_agent_slugs_are_portable_names(value):
    assert config.validate_slug(value) == value


@pytest.mark.parametrize("value", ["", "Acme Corp", "-agent", "agent-", "under_score", "a" * 64])
def test_invalid_slugs_are_rejected(value):
    with pytest.raises(config.ConfigError, match="lowercase"):
        config.validate_slug(value)
