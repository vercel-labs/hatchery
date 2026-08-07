"""The parity scan: which js e2e tests have no python counterpart.

Plain code, no llm. `scan()` spins up a Vercel Sandbox with the js workflow
repo as its git source, clones the python repo next to it, dumps the test
files, and diffs test declarations by normalized name (`fooBarWorkflow`
matches `test_foo_bar_workflow`). The python side has no e2e tests yet, so
today the report lists every js test; the porting agent will consume this
diff one test at a time.

Parsing is regex over file contents, so exotic vitest constructs (deeply
nested `test.each` tables) can be missed — fine for a signal, the porting
step reads the real files anyway.
"""

import dataclasses
import datetime
import re

from vercel import sandbox

JS_REPO = "https://github.com/vercel/workflow.git"
JS_TESTS = "/vercel/workflow/packages/core/e2e"  # GitSource clones to /vercel/<repo name>
JS_FILTER = '-name "*.test.ts"'
PY_REPO = "https://github.com/vercel/vercel-py.git"
PY_CLONE = "/tmp/vercel-py"
PY_FILTER = '-path "*/e2e/*" -name "test_*.py"'

# test('title'), it("title"), test.skipIf(cond)('title'), test.each([..])('%s')
_VITEST = re.compile(
    r"\b(?:test|it)\b(?:\s*\.\s*\w+)*\s*(?:\((?:[^()]|\([^()]*\))*\))?\s*"
    r"\(\s*(['\"`])((?:(?!\1)[^\n])+?)\1"
)
_PYTEST = re.compile(r"^[ \t]*(?:async\s+)?def\s+(test_\w+)", re.M)


@dataclasses.dataclass(frozen=True)
class Test:
    file: str
    title: str


@dataclasses.dataclass
class Report:
    js: list[Test]
    py: list[Test]

    @property
    def missing(self) -> list[Test]:
        ported = {_slug(t.title) for t in self.py}
        return [t for t in self.js if _slug(t.title) not in ported]

    def summary(self) -> str:
        missing = self.missing
        head = (
            f"e2e parity: {len(self.js)} js tests, {len(self.py)} python tests, "
            f"{len(missing)} missing in python"
        )
        if not missing:
            return head
        by_file: dict[str, list[str]] = {}
        for t in missing:
            by_file.setdefault(t.file, []).append(t.title)
        lines = [head, ""]
        for file, titles in sorted(by_file.items()):
            lines.append(f"`{file}` ({len(titles)}): " + ", ".join(titles))
        return "\n".join(lines)


async def scan() -> Report:
    async with sandbox.create_sandbox(
        source=sandbox.GitSource(url=JS_REPO, depth=1),
        execution_time_limit=datetime.timedelta(minutes=5),
    ) as box:
        await box.run_process(
            "git", ["clone", "--depth=1", PY_REPO, PY_CLONE], capture_output=True, check=True
        )
        js_files = await _files(box, JS_TESTS, JS_FILTER)
        py_files = await _files(box, PY_CLONE, PY_FILTER)
    return Report(
        js=[Test(f, m.group(2)) for f, c in js_files.items() for m in _VITEST.finditer(c)],
        py=[Test(f, m.group(1)) for f, c in py_files.items() for m in _PYTEST.finditer(c)],
    )


def _slug(title: str) -> str:
    """'promiseAllWorkflow' and 'test_promise_all_workflow' -> 'promiseallworkflow'."""
    return re.sub(r"[^a-z0-9]", "", title.lower()).removeprefix("test")


async def _files(box: sandbox.Sandbox, root: str, find_filter: str) -> dict[str, str]:
    """One round trip: dump matching files under root as a NUL-delimited stream."""
    script = (
        f"cd {root} && find . -type f {find_filter} | sort | "
        'while read -r f; do printf "\\000%s\\n" "$f"; cat "$f"; done'
    )
    done = await box.run_process("sh", ["-c", script], capture_output=True, check=True)
    files = {}
    for chunk in (done.stdout or "").split("\x00")[1:]:
        path, _, content = chunk.partition("\n")
        files[path.removeprefix("./")] = content
    return files
