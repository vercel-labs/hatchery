"""Keep a long-lived thread inside the model's context window.

Before a model call crosses its prospective input budget, the older part of history
is summarized into one handoff message and recent messages are kept verbatim. The
cut never separates a tool call from its results. Ported from agentmesh `agent/compaction.py`.
"""

import re
from typing import Any

import ai

from hatchery import model_budget
from hatchery.agent import context

Messages = list[dict[str, Any]]
MARKER = "[Context compacted] Earlier turns were summarized into this handoff:\n\n"
SKILL_MARKER = "[SKILL_PRUNED: reload with skill_view(name='{name}')]"
SKILL_MARKER_NAME = re.compile(r"\[SKILL_PRUNED: reload with skill_view\(name='([^']+)'\)\]")
MAX_SKILL_MARKERS = 20


class CompactionTooLarge(RuntimeError):
    """No non-empty, role-safe history prefix fits in a compaction request."""


def due(estimated_input_tokens: int, message_count: int, *, above: int, keep: int) -> bool:
    return estimated_input_tokens > above and message_count > keep + 1


def split(messages: Messages, keep: int) -> tuple[Messages, Messages]:
    """(head to summarize, tail to keep). The tail starts on a user or assistant message."""
    cut = max(0, len(messages) - keep)
    while cut > 0 and messages[cut].get("role") == "tool":
        cut -= 1
    return messages[:cut], messages[cut:]


def transcript(head: Messages) -> str:
    lines = []
    for raw in head:
        message = ai.messages.Message.model_validate(raw)
        if message.role == "user" and message.text.startswith(MARKER):
            lines.append(f"[previous handoff]\n{message.text.removeprefix(MARKER)}")
            continue
        if message.text:
            source = raw.get("source")
            role = (
                "parent"
                if source == "parent"
                else "signal"
                if source == "signal"
                else "task"
                if source == "task"
                else message.role
            )
            lines.append(f"[{role}] {message.text}")
        for call in getattr(message, "tool_calls", []):
            lines.append(f"[{message.role} -> {call.tool_name}] {call.tool_args}")
        for part in getattr(message, "tool_results", []):
            model_input = part.get_model_input()
            value = model_input if isinstance(model_input, str) else str(model_input)
            lines.append(f"[{part.tool_name} result] {value[:4000]}")
    return "\n".join(lines)


def loaded_skills(messages: Messages) -> set[str]:
    names = set()
    for raw in messages:
        message = ai.messages.Message.model_validate(raw)
        for part in message.tool_results:
            if part.tool_name != "skill_view" or part.is_error or not isinstance(part.result, dict):
                continue
            name = part.result.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def pruned_skills(messages: Messages) -> set[str]:
    names = loaded_skills(messages)
    for raw in messages:
        names.update(SKILL_MARKER_NAME.findall(ai.messages.Message.model_validate(raw).text))
    return names


async def compact(
    model: ai.Model,
    messages: Messages,
    *,
    keep: int,
    max_input_tokens: int,
    max_output_tokens: int,
) -> tuple[Messages, ai.types.usage.Usage]:
    """Summarize everything before the kept tail into one handoff message."""
    head, tail = split(messages, keep)
    if not head:
        return messages, ai.types.usage.Usage()
    system = context.prompt("compaction").template
    cut = len(head)
    while cut:
        user = ai.user_message(transcript(head[:cut]))
        raw_user = user.model_dump(mode="json")
        if model_budget.request_tokens(system, [raw_user], []) <= max_input_tokens:
            break
        cut -= 1
        while cut and messages[cut].get("role") == "tool":
            cut -= 1
    if not cut:
        raise CompactionTooLarge("oldest conversation group exceeds the model input limit")
    history = [ai.system_message(system), user]
    params = ai.InferenceRequestParams(output=ai.OutputParams(max_tokens=max_output_tokens))
    async with ai.stream(model, history, params=params) as response:
        async for _ in response:
            pass
    pruned = pruned_skills(head[:cut]) - loaded_skills([*head[cut:], *tail])
    reloads = "\n".join(
        SKILL_MARKER.format(name=name) for name in sorted(pruned)[:MAX_SKILL_MARKERS]
    )
    summary_text = response.message.text.strip() + (f"\n\n{reloads}" if reloads else "")
    summary = ai.user_message(MARKER + summary_text)
    usage = response.message.usage or response.usage or ai.types.usage.Usage()
    return [summary.model_dump(mode="json"), *head[cut:], *tail], usage
