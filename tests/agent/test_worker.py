import dataclasses
import json
from unittest import mock

from agent import parity, worker


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
    report = parity.Report(
        js=[parity.Test("stream.test.ts", "fooWorkflow"), parity.Test("stream.test.ts", "barWorkflow")],
        py=[parity.Test("e2e/test_foo.py", "test_foo_workflow")],
    )
    box = FakeBox()
    monkeypatch.setattr(worker.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    monkeypatch.setattr(parity, "scan", mock.AsyncMock(return_value=report))
    payload = await worker.scan_step.func("box-1")
    assert payload["js_total"] == 2
    assert payload["py_total"] == 1
    assert [t["title"] for t in payload["missing"]] == ["barWorkflow"]
    json.dumps(payload)  # step i/o must survive the journal


async def test_bash_step_reports_exit_code_and_stderr(monkeypatch):
    box = FakeBox(FakeProcess(stdout="out", stderr="err", returncode=2))
    monkeypatch.setattr(worker.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    output = await worker.bash_step.func("box-1", "false", 120)
    assert output == "[exit code 2]\nout\nerr"
    assert box.calls == [("bash", ("-lc", "false"))]


async def test_bash_step_clips_long_output(monkeypatch):
    box = FakeBox(FakeProcess(stdout="x" * (worker.OUTPUT_LIMIT + 100)))
    monkeypatch.setattr(worker.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    output = await worker.bash_step.func("box-1", "yes", 120)
    assert output.endswith("[truncated 100 chars]")


async def test_file_steps_use_sandbox_fs(monkeypatch):
    box = FakeBox()
    box.fs.read_text.return_value = "content"
    monkeypatch.setattr(worker.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    assert await worker.read_file_step.func("box-1", "/tmp/a.py") == "content"
    assert await worker.write_file_step.func("box-1", "/tmp/b.py", "hi") == "wrote 2 chars to /tmp/b.py"
    box.fs.write_text.assert_awaited_once_with("/tmp/b.py", "hi")


async def test_teardown_step_destroys(monkeypatch):
    box = FakeBox()
    monkeypatch.setattr(worker.sandbox, "get_sandbox", mock.AsyncMock(return_value=box))
    await worker.teardown_step.func("box-1")
    assert box.destroyed


def test_sandbox_tools_hide_the_sandbox_name():
    tools = worker.sandbox_tools("box-1")
    assert [t.name for t in tools] == ["bash", "read_file", "write_file"]
    for t in tools:
        assert "sandbox_name" not in t.tool.spec.params.get("properties", {})


def test_system_prompt_communicates_read_only():
    assert "READ-ONLY" in worker.SYSTEM_PROMPT
    assert "gh" in worker.SYSTEM_PROMPT


def test_registry():
    assert worker.parity_workflow.workflow_id in worker.workflow._workflows
    for step in (
        worker.setup_step,
        worker.scan_step,
        worker.llm_step,
        worker.bash_step,
        worker.read_file_step,
        worker.write_file_step,
        worker.teardown_step,
    ):
        assert step.name in worker.workflow._steps
