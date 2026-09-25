"""Prospective model-request sizing against provider context limits.

Ported from agentmesh `model_budget.py`.
"""

import collections.abc
import dataclasses
import functools
import json
import math
import typing

import modelsdotdev
from ai.types import messages as ai_messages
from ai.types import tools as ai_tools


@dataclasses.dataclass(frozen=True)
class ModelLimits:
    context_tokens: int
    output_tokens: int
    input_tokens: int | None = None

    def input_limit(self, output_reserve: int) -> int:
        if output_reserve > self.output_tokens:
            raise ValueError(
                f"configured model output {output_reserve} exceeds the provider limit "
                f"of {self.output_tokens} tokens"
            )
        limit = self.context_tokens - output_reserve
        if self.input_tokens is not None:
            limit = min(limit, self.input_tokens)
        if limit <= 0:
            raise ValueError("model output reserve leaves no room for input")
        return limit


@functools.lru_cache(maxsize=64)
def resolve_limits(model_id: str, *, context_override: int | None = None) -> ModelLimits:
    """Resolve a Gateway model through models.dev, with an override for custom models."""
    model = modelsdotdev.get_model_by_id(f"vercel:{model_id}")
    if model is None and context_override is None:
        raise ValueError(
            f"no context-window metadata for model {model_id!r}; set "
            "HATCHERY_MODEL_CONTEXT_WINDOW_TOKENS explicitly"
        )
    if model is None:
        assert context_override is not None
        return ModelLimits(context_override, context_override)
    return ModelLimits(
        context_tokens=context_override or model.limits.context,
        output_tokens=model.limits.output,
        input_tokens=model.limits.input,
    )


def estimate_tokens(value: typing.Any) -> int:
    """Conservative portable estimate where no provider tokenizer is available."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return max(1, math.ceil(len(encoded) / 3))


def message_input(message: ai_messages.Message) -> dict[str, typing.Any]:
    """The serialized message projection providers see, excluding full tool results."""
    value = message.model_dump(mode="json")
    for index, part in enumerate(message.parts):
        if not isinstance(part, ai_messages.ToolResultPart):
            continue
        projected = value["parts"][index]
        projected["result"] = part.get_model_input()
        projected.pop("model_input", None)
        projected.pop("model_input_kind", None)
    return value


def request_tokens(
    system: str,
    messages: collections.abc.Sequence[dict[str, typing.Any]],
    tools: collections.abc.Sequence[ai_tools.Tool],
) -> int:
    """Estimate the complete provider input: system, history, and tool declarations."""
    history = [message_input(ai_messages.Message.model_validate(raw)) for raw in messages]
    payload = {
        "messages": [{"role": "system", "parts": [{"kind": "text", "text": system}]}] + history,
        "tools": [tool.model_dump(mode="json") for tool in tools],
    }
    return estimate_tokens(payload)
