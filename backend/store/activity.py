"""Append-only subagent activity, one stream per subagent."""

import typing

from store import events, subagents


async def append(
    launch_id: str,
    kind: str,
    data: dict[str, typing.Any],
    *,
    source_cursor: str | None = None,
) -> int:
    """Store one normalized DevBox event and return its local cursor."""
    event = {
        "kind": kind,
        "summary": _summary(kind, data),
        "data": data,
    }
    if source_cursor:
        event["source_cursor"] = source_cursor
    return await events.append(launch_id, "activity", event)


async def cursor(launch_id: str) -> int:
    found = await events.read(launch_id, "activity")
    return found[-1][0] if found else -1


async def status(
    chat_id: str,
    launch_id: str | None = None,
    *,
    after: int | None = None,
    limit: int = 20,
) -> dict[str, typing.Any]:
    """Return a bounded status view for one of the chat's subagents."""
    launches = await subagents.list_for_chat(chat_id)
    if launch_id is None:
        if not launches:
            return {"state": "idle", "events": [], "cursor": None}
        record = launches[-1]
    else:
        record = next((item for item in launches if item["id"] == launch_id), None)
        if record is None:
            raise ValueError("subagent does not belong to this chat")

    start = 0 if after is None else after + 1
    found = await events.read(record["id"], "activity", start)
    bounded = found[: max(1, min(limit, 50))]
    return {
        "subagent_id": record["id"],
        "task_id": record.get("task_id"),
        "title": record.get("title"),
        "state": record.get("state", "unknown"),
        "cursor": bounded[-1][0] if bounded else after,
        "has_more": len(found) > len(bounded),
        "events": [
            {
                "cursor": index,
                "kind": event.get("kind", "other"),
                "summary": event.get("summary", "subagent activity"),
            }
            for index, event in bounded
        ],
        "result": _result(record.get("result"))
        if record.get("state") in ("complete", "errored")
        else None,
    }


def _result(raw: typing.Any) -> dict[str, typing.Any] | None:
    if not isinstance(raw, dict):
        return None
    result = {
        key: raw[key]
        for key in ("summary", "error")
        if raw.get(key) is not None
    }
    prs = raw.get("prs")
    if isinstance(prs, list):
        result["prs"] = [
            {key: item[key] for key in ("url", "number", "title") if item.get(key) is not None}
            for item in prs[:20]
            if isinstance(item, dict)
        ]
    return result


def _summary(kind: str, data: dict[str, typing.Any]) -> str:
    if kind == "state_transition":
        return f"state changed to {data.get('to') or data.get('state') or 'unknown'}"
    if kind != "assistant_event":
        return kind.replace("_", " ")

    name = str(data.get("name", "other"))
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    if name == "assistant_message":
        return str(body.get("text") or "assistant update")[:500]
    if name == "tool_call":
        tool = str(body.get("tool") or "tool")
        tool_input = body.get("input") if isinstance(body.get("input"), dict) else {}
        input_body = tool_input.get("body") if isinstance(tool_input.get("body"), dict) else {}
        detail = input_body.get("path") or input_body.get("command") or input_body.get("name")
        return f"{tool}: {detail}"[:500] if detail else f"using {tool}"
    if name == "tool_result":
        return "tool failed" if body.get("is_error") else "tool finished"
    if name == "attention_required":
        return f"needs attention: {body.get('prompt') or 'input required'}"[:500]
    if name == "complete":
        return str(body.get("summary") or "subagent completed the task")[:500]
    if name == "agent_error":
        return f"agent error: {body.get('message') or 'unknown error'}"[:500]
    return name.replace("_", " ")
