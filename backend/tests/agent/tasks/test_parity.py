import asyncio
import contextlib
import dataclasses
import json
from unittest import mock

import pytest

from agent import telemetry, turn
from agent.tasks import parity, scan


@dataclasses.dataclass
class FakeProcess:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class FakeBox:
    def __init__(self, process: FakeProcess | None = None) -> None:
        self.process = process or FakeProcess()
        self.calls: list[tuple[str, tuple]] = []
        self.fs = mock.AsyncMock()
        self.destroyed = False

    async def run_process(self, command, args=None, **kwargs):
        self.calls.append((command, tuple(args or ())))
        return self.process

    async def destroy(self):
        self.destroyed = True


async def test_scan_step_payload(monkeypatch):
    report = scan.Report(
        js=[scan.Test("stream.test.ts", "fooWorkflow"), scan.Test("stream.test.ts", "barWorkflow")],
        py=[scan.Test("e2e/test_foo.py", "test_foo_workflow")],
    )
    box = FakeBox()
    monkeypatch.setattr(parity.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    monkeypatch.setattr(scan, "scan", mock.AsyncMock(return_value=report))
    payload = await parity.scan_step.func("box-1")
    assert payload["js_total"] == 2
    assert payload["py_total"] == 1
    assert [t["title"] for t in payload["missing"]] == ["barWorkflow"]
    json.dumps(payload)  # step i/o must survive the journal


async def test_bash_step_reports_exit_code_and_stderr(monkeypatch):
    box = FakeBox(FakeProcess(stdout="out", stderr="err", returncode=2))
    monkeypatch.setattr(parity.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    output = await parity.bash_step.func("box-1", "false", 120)
    assert output == "[exit code 2]\nout\nerr"
    assert box.calls == [("bash", ("-lc", "false"))]


async def test_bash_step_clips_long_output(monkeypatch):
    box = FakeBox(FakeProcess(stdout="x" * (parity.OUTPUT_LIMIT + 100)))
    monkeypatch.setattr(parity.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    output = await parity.bash_step.func("box-1", "yes", 120)
    assert output.endswith("[truncated 100 chars]")


async def test_file_steps_use_sandbox_fs(monkeypatch):
    box = FakeBox()
    box.fs.read_text.return_value = "content"
    monkeypatch.setattr(parity.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    assert await parity.read_file_step.func("box-1", "/tmp/a.py") == "content"
    assert await parity.write_file_step.func("box-1", "/tmp/b.py", "hi") == "wrote 2 chars to /tmp/b.py"
    box.fs.write_text.assert_awaited_once_with("/tmp/b.py", "hi")


async def test_teardown_step_destroys(monkeypatch):
    box = FakeBox()
    monkeypatch.setattr(parity.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    await parity.teardown_step.func("box-1")
    assert box.destroyed


async def test_llm_step_exports_spans_under_serialized_parent(monkeypatch):
    class FakeModelStream:
        message = parity.ai.assistant_message("done")

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    @contextlib.asynccontextmanager
    async def fake_stream(*args, **kwargs):
        async with parity.ai.experimental_telemetry.span("fake_model"):
            yield FakeModelStream()

    monkeypatch.setattr(parity.ai, "stream", fake_stream)
    sink = parity.ai.experimental_telemetry.DictSink()
    async with parity.ai.experimental_telemetry.use_sink(sink):
        parent = parity.ai.experimental_telemetry.create_span("agent").stamp_start()

    child_sink = parity.ai.experimental_telemetry.DictSink()
    async with parity.ai.experimental_telemetry.use_sink(child_sink):
        result = await parity.llm_step.func(
            parity.ai.get_model(parity.MODEL_ID).model_dump(mode="json"),
            [parity.ai.user_message("hello").model_dump(mode="json")],
            [],
            parent.model_dump(mode="json"),
        )

    assert parity.ai.messages.Message.model_validate(result).text == "done"
    [span] = child_sink.finished_spans
    assert span.name == "fake_model"
    assert span.parent_id == parent.id
    assert span.trace_id == parent.trace_id


async def test_finish_trace_pushes_and_flushes(monkeypatch):
    push_all = mock.AsyncMock()
    flush = mock.Mock()
    monkeypatch.setattr(parity.ai.experimental_telemetry, "push_all", push_all)
    monkeypatch.setattr(telemetry, "flush", flush)
    sink = parity.ai.experimental_telemetry.DictSink()
    async with parity.ai.experimental_telemetry.use_sink(sink):
        trace = parity.ai.experimental_telemetry.create_span("root").stamp_start()

    spans = [{"id": "span-1"}]
    await parity.finish_trace_step.func(
        spans,
        trace.model_dump(mode="json"),
        "done",
        None,
        None,
    )

    push_all.assert_awaited_once_with(spans)
    flush.assert_called_once_with()


async def test_workflow_suspension_does_not_finish_trace_or_deliver(monkeypatch):
    monkeypatch.setattr(parity, "start_trace_step", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(
        parity, "setup_step", mock.AsyncMock(side_effect=asyncio.CancelledError)
    )
    teardown = mock.AsyncMock()
    finish_trace = mock.AsyncMock()
    emit = mock.AsyncMock()
    monkeypatch.setattr(parity, "teardown_step", teardown)
    monkeypatch.setattr(parity, "finish_trace_step", finish_trace)
    monkeypatch.setattr(turn, "emit_step", emit)
    workflow_body = parity.parity_workflow.func.__wrapped__.__wrapped__

    with pytest.raises(asyncio.CancelledError):
        await workflow_body("cht_1")

    teardown.assert_not_awaited()
    finish_trace.assert_not_awaited()
    emit.assert_not_awaited()


def test_sandbox_tools_hide_the_sandbox_name():
    tools = parity.sandbox_tools("box-1")
    assert [t.name for t in tools] == ["bash", "read_file", "write_file"]
    for t in tools:
        assert "sandbox_name" not in t.tool.spec.params.get("properties", {})


def test_system_prompt_communicates_read_only():
    assert "READ-ONLY" in parity.SYSTEM_PROMPT
    assert "gh" in parity.SYSTEM_PROMPT


def test_registry():
    assert parity.parity_workflow.workflow_id in parity.workflow._workflows
    for step in (
        parity.setup_step,
        parity.scan_step,
        parity.llm_step,
        parity.bash_step,
        parity.read_file_step,
        parity.write_file_step,
        parity.teardown_step,
        parity.start_trace_step,
        parity.finish_trace_step,
    ):
        assert step.name in parity.workflow._steps
