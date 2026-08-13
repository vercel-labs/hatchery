import dataclasses

from agent.tasks import scan

VITEST = """\
describe('workflows', () => {
  test('promiseAllWorkflow', { timeout: 60_000 }, async () => {});
  it("sleepingWorkflow", async () => {});
  test.skipIf(!!process.env.WORKFLOW_VERCEL_ENV)(
    'webhook route with invalid token',
    async () => {},
  );
  test.each([1, 2])('nullByteWorkflow %s', async () => {});
  test(
    'streamWorkflow',
    { timeout: 120_000 },
    async () => {},
  );
  notATest('nope');
});
"""

PYTEST = """\
import pytest

async def test_promise_all_workflow():
    pass

def test_sleeping_workflow():
    pass

def helper():
    pass

class TestGroup:
    async def test_stream_workflow(self):
        pass
"""


def report(js_content: str = VITEST, py_content: str = PYTEST) -> scan.Report:
    js = [scan.Test("e2e.test.ts", m.group(2)) for m in scan._VITEST.finditer(js_content)]
    py = [scan.Test("test_e2e.py", m.group(1)) for m in scan._PYTEST.finditer(py_content)]
    return scan.Report(js=js, py=py)


def test_vitest_parser_finds_declarations():
    titles = [t.title for t in report().js]
    assert titles == [
        "promiseAllWorkflow",
        "sleepingWorkflow",
        "webhook route with invalid token",
        "nullByteWorkflow %s",
        "streamWorkflow",
    ]


def test_pytest_parser_finds_test_functions():
    titles = [t.title for t in report().py]
    assert titles == ["test_promise_all_workflow", "test_sleeping_workflow", "test_stream_workflow"]


def test_slug_matches_js_titles_to_python_names():
    assert scan._slug("promiseAllWorkflow") == scan._slug("test_promise_all_workflow")
    assert scan._slug("webhook route with invalid token") == scan._slug(
        "test_webhook_route_with_invalid_token"
    )
    assert scan._slug("fooWorkflow") != scan._slug("test_bar_workflow")


def test_missing_diffs_by_slug():
    missing = [t.title for t in report().missing]
    assert missing == ["webhook route with invalid token", "nullByteWorkflow %s"]


def test_summary_groups_by_file():
    text = report().summary()
    assert text.startswith("e2e parity: 5 js tests, 3 python tests, 2 missing in python")
    assert "`e2e.test.ts` (2): webhook route with invalid token, nullByteWorkflow %s" in text


def test_summary_when_nothing_missing():
    full = report(js_content="test('promiseAllWorkflow', fn);")
    assert full.summary() == "e2e parity: 1 js tests, 3 python tests, 0 missing in python"


@dataclasses.dataclass
class FakeProcess:
    stdout: str
    returncode: int = 0


class FakeBox:
    """Stands in for sandbox.Sandbox: records commands, replays canned dumps."""

    def __init__(self, dumps: list[str]) -> None:
        self.dumps = dumps
        self.calls: list[tuple[str, tuple]] = []

    async def run_process(self, command, args=None, **kwargs):
        self.calls.append((command, tuple(args or ())))
        return FakeProcess(stdout=self.dumps.pop(0) if command == "sh" else "")


async def test_scan_dumps_and_diffs():
    js_dump = "\x00./e2e.test.ts\ntest('fooWorkflow', fn);\ntest('barWorkflow', fn);"
    py_dump = "\x00./src/vercel/tests/e2e/test_e2e.py\ndef test_foo_workflow():\n    pass"
    box = FakeBox(dumps=[js_dump, py_dump])

    result = await scan.scan(box)

    assert result.js == [
        scan.Test("e2e.test.ts", "fooWorkflow"),
        scan.Test("e2e.test.ts", "barWorkflow"),
    ]
    assert result.py == [scan.Test("src/vercel/tests/e2e/test_e2e.py", "test_foo_workflow")]
    assert [t.title for t in result.missing] == ["barWorkflow"]


async def test_scan_with_no_python_tests():
    box = FakeBox(dumps=["\x00./e2e.test.ts\ntest('fooWorkflow', fn);", ""])

    result = await scan.scan(box)

    assert result.py == []
    assert [t.title for t in result.missing] == ["fooWorkflow"]
