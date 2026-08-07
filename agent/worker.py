"""The durable parity agent: a workflow whose llm calls are steps.

Port of ai-python's durable_agent_workflows example, trimmed to this bot.
The scan runs first as a plain step, then the agent is invoked with the
scan as its initial user message. Each llm call is a @workflow.step, so a
crash resumes mid-conversation instead of rerunning the sandbox.

No tools yet: today's agent only analyzes the scan and reports. The
pr-making agent adds sandbox/github tools to ParityAgent.TOOLS — each one
a @workflow.step like llm_step (max_retries=0 for non-idempotent ones).
"""

import dataclasses
import json
import typing

import ai
import vercel.workflow

from agent import parity

workflow = vercel.workflow.Workflows(
    sandbox_policy=vercel.workflow.SandboxPolicy(
        passthrough_modules=frozenset({"ai"}),
        cleanups=vercel.workflow.sandbox.ALL_CLEANUPS,
    )
)

MODEL_ID = "gateway:anthropic/claude-sonnet-4.6"
SYSTEM_PROMPT = """\
You are the e2e parity bot for vercel's python sdks. You receive a scan
comparing e2e tests in the js workflow sdk (vercel/workflow) with the
python sdk (vercel/vercel-py).

Write a short parity report:
- how many js tests exist, how many python tests exist, how many are
  missing in python
- one or two sentences on what the missing tests cover
- note that the porting agent is still being built: this is a test run,
  and it is expected that all js tests are currently missing in python

Be terse. Plain sentences, no headers.
"""


@workflow.step
async def scan_step() -> dict:
    report = await parity.scan()
    return {
        "js_total": len(report.js),
        "py_total": len(report.py),
        "missing": [dataclasses.asdict(t) for t in report.missing],
    }


@workflow.step
async def llm_step(
    model_data: dict[str, object],
    messages_data: list[dict[str, object]],
    tools_data: list[dict[str, object]],
) -> dict[str, object]:
    model = ai.Model.model_validate(model_data)
    messages = [ai.messages.Message.model_validate(m) for m in messages_data]
    tools = [ai.Tool.model_validate(t) for t in tools_data]
    async with ai.stream(model, messages, tools=tools) as model_stream:
        async for _event in model_stream:
            pass
    return model_stream.message.model_dump(mode="json")


class ParityAgent(ai.Agent):
    TOOLS: typing.ClassVar[list[ai.AgentTool]] = []  # sandbox/github tools land here

    async def loop(self, context: ai.Context) -> typing.AsyncGenerator[ai.events.AgentEvent, None]:
        tools_data = [tool.model_dump(mode="json") for tool in context.tools]
        while context.keep_running():
            result = await llm_step(
                context.model.model_dump(mode="json"),
                [message.model_dump(mode="json") for message in context.messages],
                tools_data,
            )
            assistant_message = ai.messages.Message.model_validate(result)
            context.add(assistant_message)

            async with ai.Stream.replay_message(assistant_message) as replay:
                async for event in replay:
                    yield event

            async with ai.ToolRunner() as runner:
                for tool_call in assistant_message.tool_calls:
                    runner.schedule(context.resolve(tool_call))
                async for tool_event in runner.events():
                    yield tool_event
                tool_message = runner.get_tool_message()
                if tool_message is not None:
                    context.add(tool_message)


parity_agent = ParityAgent()


@workflow.workflow
@ai.messages.use_random(vercel.workflow.random)
async def parity_workflow() -> str:
    scan = await scan_step()
    messages = [
        ai.system_message(SYSTEM_PROMPT),
        ai.user_message("today's parity scan:\n" + json.dumps(scan)),
    ]
    async with parity_agent.run(ai.get_model(MODEL_ID), messages) as run:
        async for _event in run:
            pass
        return run.messages[-1].text
