import ai
import pytest

from hatchery import config, model_budget


def test_request_budget_uses_model_preview_and_counts_system_history_and_tools():
    part = ai.types.messages.ToolResultPart(
        tool_call_id="call-budget",
        tool_name="bash",
        result={"stdout": "full" * 50_000},
        model_input={"stdout": "bounded"},
    )
    raw = ai.tool_message(part).model_dump(mode="json")
    projected = model_budget.message_input(ai.types.messages.Message.model_validate(raw))
    base = model_budget.request_tokens("short", [raw], [])

    @ai.tool
    async def bash(command: str) -> str:
        """Run a command."""
        return command

    assert projected["parts"][0]["result"] == {"stdout": "bounded"}
    assert "model_input" not in projected["parts"][0]
    assert model_budget.request_tokens("long " * 1000, [raw], []) > base
    assert model_budget.request_tokens("short", [raw], [bash.tool]) > base
    assert base < 1000, "the full durable result must not enter the request estimate"


def test_model_limits_reserve_output_and_honor_an_independent_input_limit():
    limits = model_budget.ModelLimits(context_tokens=10_000, output_tokens=2000, input_tokens=7000)
    assert limits.input_limit(1000) == 7000
    assert model_budget.ModelLimits(10_000, 2000).input_limit(1000) == 9000
    with pytest.raises(ValueError, match="provider limit"):
        limits.input_limit(3000)


def test_hatchery_default_model_has_models_dev_limits():
    limits = model_budget.resolve_limits(config.ModelConfig().id)
    assert limits.context_tokens > limits.output_tokens >= config.ModelConfig().max_output_tokens
    assert limits.input_limit(config.ModelConfig().max_output_tokens) > 100_000


def test_custom_model_context_override_supplies_missing_provider_metadata():
    limits = model_budget.resolve_limits("custom/model-without-metadata", context_override=32_000)
    assert limits == model_budget.ModelLimits(context_tokens=32_000, output_tokens=32_000)
    with pytest.raises(ValueError, match="CONTEXT_WINDOW_TOKENS"):
        model_budget.resolve_limits("custom/model-without-metadata")
