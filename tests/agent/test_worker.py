import json
from unittest import mock

from agent import parity, worker


async def test_scan_step_payload(monkeypatch):
    report = parity.Report(
        js=[parity.Test("stream.test.ts", "fooWorkflow"), parity.Test("stream.test.ts", "barWorkflow")],
        py=[parity.Test("e2e/test_foo.py", "test_foo_workflow")],
    )
    monkeypatch.setattr(parity, "scan", mock.AsyncMock(return_value=report))
    payload = await worker.scan_step.func()
    assert payload["js_total"] == 2
    assert payload["py_total"] == 1
    assert [t["title"] for t in payload["missing"]] == ["barWorkflow"]
    json.dumps(payload)  # step i/o must survive the journal


def test_registry():
    assert worker.parity_workflow.workflow_id in worker.workflow._workflows
    assert worker.scan_step.name in worker.workflow._steps
    assert worker.llm_step.name in worker.workflow._steps
