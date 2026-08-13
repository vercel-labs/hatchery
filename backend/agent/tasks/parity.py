"""The durable parity agent: a workflow whose llm and tool calls are steps.

Port of ai-python's durable_agent_workflows example, trimmed to this bot.
Setup creates one named sandbox per run with both repos in it, the scan runs
against it as a plain step, then the agent is invoked with the scan as its
initial user message. Each llm call and each tool call is a @workflow.step,
so a crash resumes mid-conversation instead of rerunning the sandbox.

Tools are closures bound to the run's sandbox name (`sandbox_tools`): the
model sees only the tool's own params, the name rides along to a module-level
step that reattaches via get_sandbox. Read-only for now — the system prompt
forbids anything that writes outside the sandbox; the pr-making run relaxes
that once the porting agent lands.

Progress and the final report go to the chat identified by `chat_id` via
agent.turn.emit_step — the UI stream and every channel binding at once.
"""

import dataclasses
import datetime
import json
import os
import typing

import ai
import vercel.workflow
from ai.providers.anthropic import tools as anthropic_tools
from vercel import connect, sandbox

import agent
from agent import turn
from agent.tasks import scan as scan_mod

workflow = agent.workflow

MODEL_ID = "gateway:anthropic/claude-sonnet-4.6"
MAX_TURNS = 50  # the first live run used 29 turns; the cap is a runaway stop, not a budget
SANDBOX_LIMIT = datetime.timedelta(minutes=30)
OUTPUT_LIMIT = 16_000  # chars of tool output the model sees per call
SYSTEM_PROMPT = """\
You are the e2e parity bot for vercel's python sdks. Your job is to compare
e2e tests in the js workflow sdk with the python sdk and report what is
missing in python.

You have a sandbox with both repos cloned:
- /vercel/workflow — js sdk; e2e tests in packages/core/e2e
- /tmp/vercel-py — python sdk; e2e tests in */e2e/ directories

Tools: bash, read_file, and write_file run inside that sandbox; web search is
available for anything you can't learn from the repos. Use the gh cli (via
bash) for all github operations — checking existing issues and prs now,
submitting prs later. gh may be unauthenticated; if a gh call fails on auth,
note it in the report instead of working around it.

READ-ONLY: this run must not change anything outside the sandbox. Do not
push, and do not run gh commands that create or edit anything — no pr
create, no issue create, no comments, edits, or reactions. Writing scratch
files inside the sandbox is fine. The porting agent that makes prs is still
being built; it is expected that most or all js tests are missing in python.

You receive today's parity scan (counts plus the missing-test list, matched
by normalized name). Spot-check a few missing tests against the real files,
check github for existing porting issues or prs, then write a short parity
report: the counts, one or two sentences on what the missing tests cover,
and anything surprising. Be terse. Plain sentences, no headers.

Note that this is a test run. If something doesn't work as stated here, e.g.
repos aren't where they belong, tools don't work, etc., include that with details
in the report for debugging.
"""


@workflow.step
async def setup_step() -> str:
    """Create this run's sandbox with both repos in it; return its name.

    Named after the run id so a step retry reattaches instead of leaking a
    second sandbox; the re-clone of the python repo is idempotent for the
    same reason. SANDBOX_LIMIT reaps the session if the run dies before
    teardown_step.
    """
    run_id = vercel.workflow.get_step_metadata().run_id
    env = {}
    if os.environ.get("GITHUB_CONNECTOR"):
        try:
            env["GH_TOKEN"] = await connect.get_token(
                os.environ["GITHUB_CONNECTOR"], subject=connect.ConnectAppTokenSubject()
            )
        except Exception:
            pass  # public-repo reads still work; the agent reports gh as unauthenticated
    box, _created = await sandbox.get_or_create_sandbox(
        name="e2e-bot-" + run_id.lower().replace("_", "-"),
        source=sandbox.GitSource(url=scan_mod.JS_REPO, depth=1),
        execution_time_limit=SANDBOX_LIMIT,
        persistent=True,
        env=env,
    )
    clone = f"rm -rf {scan_mod.PY_CLONE} && git clone --depth=1 {scan_mod.PY_REPO} {scan_mod.PY_CLONE}"
    await box.run_process("sh", ["-c", clone], capture_output=True, check=True)
    return box.name


@workflow.step
async def scan_step(sandbox_name: str) -> dict:
    box = await sandbox.get_sandbox(name=sandbox_name)
    report = await scan_mod.scan(box)
    return {
        "js_total": len(report.js),
        "py_total": len(report.py),
        "missing": [dataclasses.asdict(t) for t in report.missing],
    }


@workflow.step
async def teardown_step(sandbox_name: str) -> None:
    box = await sandbox.get_sandbox(name=sandbox_name)
    await box.destroy()


@workflow.step(max_retries=0)
async def start_trace_step() -> dict[str, object] | None:
    if not ai.experimental_telemetry.is_enabled():
        return None
    trace = ai.experimental_telemetry.create_span("parity_workflow").stamp_start()
    trace.trace_attrs["braintrust.metadata.model"] = MODEL_ID
    trace.set_attrs(
        {"braintrust.input": "compare workflow e2e tests across js and python"}
    )
    return trace.model_dump(mode="json")


@workflow.step(max_retries=0)
async def finish_trace_step(
    spans_data: list[dict[str, object]],
    trace_data: dict[str, object] | None,
    report: str | None,
    error_type: str | None,
    error_message: str | None,
) -> None:
    import agent.telemetry as telemetry

    await ai.experimental_telemetry.push_all(spans_data)
    if trace_data is not None:
        trace = ai.experimental_telemetry.Span.model_validate(trace_data)
        if report is not None:
            trace.set_attrs({"braintrust.output": report})
        trace.stamp_end(
            error=ai.experimental_telemetry.SpanError(
                type=error_type,
                message=error_message or "",
            )
            if error_type is not None
            else None
        )
        await trace.push()
    telemetry.flush()


@workflow.step(max_retries=0)  # bash isn't idempotent: let the agent see the failure
async def bash_step(sandbox_name: str, command: str, timeout: int) -> str:
    box = await sandbox.get_sandbox(name=sandbox_name)
    done = await box.run_process(
        "bash", ["-lc", command], capture_output=True, kill_after=timeout
    )
    output = done.stdout or ""
    if done.stderr:
        output += ("\n" if output else "") + done.stderr
    if done.returncode != 0:
        output = f"[exit code {done.returncode}]\n{output}"
    return _clip(output)


@workflow.step
async def read_file_step(sandbox_name: str, path: str) -> str:
    box = await sandbox.get_sandbox(name=sandbox_name)
    return _clip(await box.fs.read_text(path))


@workflow.step
async def write_file_step(sandbox_name: str, path: str, content: str) -> str:
    box = await sandbox.get_sandbox(name=sandbox_name)
    await box.fs.write_text(path, content)
    return f"wrote {len(content)} chars to {path}"


def _clip(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + f"\n[truncated {len(text) - OUTPUT_LIMIT} chars]"


def sandbox_tools(sandbox_name: str) -> list[ai.AgentTool]:
    """The agent's sandbox tools, bound to this run's sandbox.

    Closures so the model never sees (or supplies) the sandbox name; the
    steps they call are module-level because the workflow registry addresses
    steps by name.
    """

    @ai.tool
    async def bash(command: str, timeout: int = 120) -> str:
        """Run a bash command in the sandbox. Both repos live there; gh, git,
        curl, node, and python3 are installed. timeout is in seconds."""
        return await bash_step(sandbox_name, command, timeout)

    @ai.tool
    async def read_file(path: str) -> str:
        """Read a text file from the sandbox. Use absolute paths."""
        return await read_file_step(sandbox_name, path)

    @ai.tool
    async def write_file(path: str, content: str) -> str:
        """Write a text file in the sandbox, replacing what is there. Use
        absolute paths."""
        return await write_file_step(sandbox_name, path, content)

    return [bash, read_file, write_file]


@workflow.step
async def llm_step(
    model_data: dict[str, object],
    messages_data: list[dict[str, object]],
    tools_data: list[dict[str, object]],
    parent_span_data: dict[str, object] | None,
) -> dict[str, object]:
    model = ai.Model.model_validate(model_data)
    messages = [ai.messages.Message.model_validate(m) for m in messages_data]
    tools = [ai.Tool.model_validate(t) for t in tools_data]
    parent_span = (
        ai.experimental_telemetry.Span.model_validate(parent_span_data)
        if parent_span_data is not None
        else None
    )
    async with (
        ai.experimental_telemetry.use_span(parent_span),
        ai.stream(model, messages, tools=tools) as model_stream,
    ):
        async for _event in model_stream:
            pass
    return model_stream.message.model_dump(mode="json")


class ParityAgent(ai.Agent):
    def __init__(
        self,
        *,
        tools: typing.Sequence[ai.AgentTool | ai.Tool],
        chat_id: str | None,
    ) -> None:
        super().__init__(tools=tools)
        self.chat_id = chat_id

    async def loop(self, context: ai.Context) -> typing.AsyncGenerator[ai.events.AgentEvent, None]:
        tools_data = [tool.model_dump(mode="json") for tool in context.tools]
        for _turn in range(MAX_TURNS):
            if not context.keep_running():
                return
            if self.chat_id is not None:
                await turn.emit_step(
                    self.chat_id,
                    "status.updated",
                    f"checking parity... turn {_turn + 1}",
                )
            parent_span = ai.experimental_telemetry.current_span()
            result = await llm_step(
                context.model.model_dump(mode="json"),
                [message.model_dump(mode="json") for message in context.messages],
                tools_data,
                parent_span.model_dump(mode="json") if parent_span is not None else None,
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


@workflow.workflow
@ai.messages.use_random(vercel.workflow.random)
@ai.experimental_telemetry.use_time(vercel.workflow.time_ns)
async def parity_workflow(
    chat_id: str | None = None,
) -> str:
    trace_data = await start_trace_step()
    trace = (
        ai.experimental_telemetry.Span.model_validate(trace_data)
        if trace_data is not None
        else None
    )
    sink = ai.experimental_telemetry.DictSink() if trace is not None else None
    box_name: str | None = None
    try:
        box_name = await setup_step()
        scan = await scan_step(box_name)
        if trace is not None:
            trace.trace_attrs.update(
                {
                    "braintrust.metadata.js_total": scan["js_total"],
                    "braintrust.metadata.py_total": scan["py_total"],
                    "braintrust.metadata.missing_count": len(scan["missing"]),
                }
            )

        parity_agent = ParityAgent(
            tools=[
                *sandbox_tools(box_name),
                anthropic_tools.web_search(max_uses=5),
            ],
            chat_id=chat_id,
        )
        messages = [
            ai.system_message(SYSTEM_PROMPT),
            ai.user_message(
                f"parity scan for {vercel.workflow.now():%Y-%m-%d}:\n"
                + json.dumps(scan)
            ),
        ]
        async with (
            ai.experimental_telemetry.use_sink(sink),
            ai.experimental_telemetry.use_span(trace),
            parity_agent.run(ai.get_model(MODEL_ID), messages) as run,
        ):
            async for _event in run:
                pass
            final = run.messages[-1]
            report = (
                final.text
                if final.role == "assistant"
                else "agent hit MAX_TURNS without a final report"
            )

        if chat_id is not None:
            await turn.emit_step(chat_id, "message.completed", report)
            await turn.emit_step(chat_id, "turn.completed", "")
        await teardown_step(box_name)
        box_name = None
    except Exception as error:
        if chat_id is not None:
            await turn.emit_step(
                chat_id,
                "turn.failed",
                f"{type(error).__name__}: {error}",
            )
        if box_name is not None:
            await teardown_step(box_name)
        finished = (
            [span.model_dump(mode="json") for span in sink.finished_spans]
            if sink is not None
            else []
        )
        await finish_trace_step(
            finished,
            trace.model_dump(mode="json") if trace is not None else None,
            None,
            type(error).__name__,
            str(error),
        )
        raise

    finished = (
        [span.model_dump(mode="json") for span in sink.finished_spans]
        if sink is not None
        else []
    )
    await finish_trace_step(
        finished,
        trace.model_dump(mode="json") if trace is not None else None,
        report,
        None,
        None,
    )
    return report
