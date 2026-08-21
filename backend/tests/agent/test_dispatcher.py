from agent import dispatcher
from store import activity, tasks


async def test_check_coder_reads_chat_activity():
    launch = await tasks.create("chat_1", "fix it", "secret")
    launch["task_id"] = "task_1"
    launch["state"] = "running"
    await tasks.save(launch)
    await activity.append(
        launch["id"],
        "assistant_event",
        {"name": "assistant_message", "body": {"text": "Found the bug"}},
    )

    agent = dispatcher.agent_for({"id": "chat_1"})
    tool = next(tool for tool in agent.tools if tool.name == "check_coder")
    result = await tool.fn()

    assert result["launch_id"] == launch["id"]
    assert result["state"] == "running"
    assert result["events"] == [
        {"cursor": 0, "kind": "assistant_event", "summary": "Found the bug"}
    ]
