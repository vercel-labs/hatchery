"""The handler SDK in real subprocesses. Ported from agentmesh `tests/unit/test_sdk.py`."""

import ast
import base64
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from hatchery import runtime, sdk

SDK = pathlib.Path(sdk.__file__).parent


def invoke(
    handler: pathlib.Path, *, env: dict[str, str] | None = None, **changes: Any
) -> subprocess.CompletedProcess[str]:
    envelope = {
        "version": 1,
        "kind": "request",
        "request_id": "request-17",
        "handler": str(handler),
        "method": "POST",
        "path": "/items/blue",
        "params": {"item": "blue"},
        "query": [["tag", "one"], ["tag", "two"]],
        "headers": [["X-Trace", "first"], ["x-trace", "second"]],
        "body_b64": base64.b64encode(b"default").decode(),
    }
    envelope.update(changes)
    return subprocess.run(
        [sys.executable, "-m", "hatchery.sdk"],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def invoke_job(handler: pathlib.Path) -> subprocess.CompletedProcess[str]:
    envelope = {
        "version": 1,
        "kind": "job",
        "run_id": "run-17",
        "handler": str(handler),
        "name": "cleanup",
        "scheduled_for": 1_765_000_000.0,
        "revision": "a" * 40,
    }
    return subprocess.run(
        [sys.executable, "-m", "hatchery.sdk"],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        check=False,
    )


def test_binary_request_and_response_preserve_repeated_values(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "binary_handler.py"
    handler.write_text(
        "from hatchery.sdk import Response\n"
        "def POST(request):\n"
        "    assert request.id == 'request-17'\n"
        "    assert request.params == {'item': 'blue'}\n"
        "    assert request.query.get_all('tag') == ['one', 'two']\n"
        "    assert request.headers['X-Trace'] == 'first'\n"
        "    assert request.headers.get_all('x-trace') == ['first', 'second']\n"
        "    return Response(request.body[::-1], status=206, "
        "headers=[('Set-Cookie', 'a=1'), ('Set-Cookie', 'b=2')])\n"
    )
    body = b"\x00\xffmesh\x80"

    result = invoke(handler, body_b64=base64.b64encode(body).decode())

    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "version": 1,
        "status": 206,
        "headers": [
            ["Set-Cookie", "a=1"],
            ["Set-Cookie", "b=2"],
            ["Content-Type", "application/octet-stream"],
        ],
        "body_b64": base64.b64encode(body[::-1]).decode(),
        "effects": [],
    }


def test_async_handler_records_prompt_effects_and_redirects_prints(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "async_handler.py"
    handler.write_text(
        "import asyncio\n"
        "from hatchery.sdk import prompt\n"
        "print('diagnostic from import')\n"
        "async def GET(request):\n"
        "    print('diagnostic from handler')\n"
        "    await asyncio.sleep(0)\n"
        "    prompt('Review the release', key='review-8', thread='thread-4')\n"
        "    return {'method': request.method, 'body': request.json()}\n"
    )

    result = invoke(handler, method="GET", body_b64=base64.b64encode(b'{"ready":true}').decode())

    assert result.returncode == 0
    assert result.stdout.count("\n") == 1
    assert "diagnostic from import" in result.stderr
    assert "diagnostic from handler" in result.stderr
    response = json.loads(result.stdout)
    assert json.loads(base64.b64decode(response["body_b64"])) == {
        "method": "GET",
        "body": {"ready": True},
    }
    assert response["effects"] == [
        {"type": "prompt", "text": "Review the release", "key": "review-8", "thread": "thread-4"}
    ]


def test_prompt_without_key_uses_request_identity_and_ordinal(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "prompt_default.py"
    handler.write_text(
        "from hatchery.sdk import prompt\n"
        "def POST(request):\n"
        "    prompt('first', key=None)\n"
        "    prompt('second')\n"
        "    return None\n"
    )

    result = invoke(handler)

    assert json.loads(result.stdout)["effects"] == [
        {"type": "prompt", "text": "first", "key": "request-17:0", "thread": "/items/blue"},
        {"type": "prompt", "text": "second", "key": "request-17:1", "thread": "/items/blue"},
    ]


def test_job_envelope_invokes_async_run_with_stable_prompt_defaults(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "job.py"
    handler.write_text(
        "import asyncio\n"
        "from hatchery.sdk import prompt\n"
        "async def run(job):\n"
        "    await asyncio.sleep(0)\n"
        "    prompt('cleanup complete')\n"
        "    return {'name': job.name, 'slot': job.scheduled_for, 'revision': job.revision}\n"
    )

    result = invoke_job(handler)

    assert result.returncode == 0 and result.stderr == ""
    response = json.loads(result.stdout)
    assert json.loads(base64.b64decode(response["body_b64"])) == {
        "name": "cleanup",
        "slot": 1_765_000_000.0,
        "revision": "a" * 40,
    }
    assert response["effects"] == [
        {
            "type": "prompt",
            "text": "cleanup complete",
            "key": "run-17:0",
            "thread": "schedule:cleanup",
        }
    ]


def test_job_exception_discards_effects_and_returns_generic_failure(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "job.py"
    handler.write_text(
        "from hatchery.sdk import prompt\n"
        "def run(job):\n"
        "    prompt('discard me')\n"
        "    raise RuntimeError('private schedule failure')\n"
    )

    result = invoke_job(handler)

    response = json.loads(result.stdout)
    assert response["status"] == 500 and response["effects"] == []
    assert base64.b64decode(response["body_b64"]) == b"Internal Server Error"
    assert "private schedule failure" in result.stderr


def test_handler_relative_files_use_persistent_data_directory(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "stateful.py"
    data = tmp_path / "data"
    data.mkdir()
    handler.write_text(
        "from pathlib import Path\n"
        "def POST(request):\n"
        "    Path('events.sqlite').write_bytes(request.body)\n"
        "    return None\n"
    )

    result = invoke(handler, env={**os.environ, "HATCHERY_DATA": str(data)})

    assert result.returncode == 0
    assert (data / "events.sqlite").read_bytes() == b"default"


def test_handler_exception_returns_generic_500_and_traceback(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "failed_handler.py"
    handler.write_text(
        "from hatchery.sdk import prompt\n"
        "def DELETE(request):\n"
        "    prompt('must be discarded', key='discard-2')\n"
        "    raise RuntimeError('private failure detail')\n"
    )

    result = invoke(handler, method="DELETE")

    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["status"] == 500 and response["effects"] == []
    assert base64.b64decode(response["body_b64"]) == b"Internal Server Error"
    assert "private failure detail" not in result.stdout
    assert "Traceback" in result.stderr and "private failure detail" in result.stderr


def test_method_without_a_handler_returns_405(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "post_only.py"
    handler.write_text("def POST(request):\n    return 'created'\n")

    result = invoke(handler, method="OPTIONS")

    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["status"] == 405
    assert base64.b64decode(response["body_b64"]) == b""


def test_request_envelope_rejects_extra_fields_without_stdout(tmp_path: pathlib.Path) -> None:
    handler = tmp_path / "unused.py"
    handler.write_text("def POST(request):\n    return 'must not run'\n")

    result = invoke(handler, unexpected="not-an-envelope-field")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "documented fields" in result.stderr


def test_public_types_validate_effects_and_normalize_bodies() -> None:
    request = sdk.Request("r-2", "PUT", "/state", body=b'{"count":3}')
    assert request.json() == {"count": 3}
    assert sdk.Response(None).status == 204
    assert sdk.Response("hello").body == b"hello"
    assert sdk.Response(["alpha", 2]).body == b'["alpha",2]'
    with pytest.raises(ValueError, match="blank"):
        sdk.prompt("  ", key="key-1")
    with pytest.raises(ValueError, match="at most 200"):
        sdk.prompt("valid", key="k" * 201)
    with pytest.raises(RuntimeError, match="handler"):
        sdk.prompt("valid", key="key-1")


def test_explicit_html_and_json_responses_set_content_types() -> None:
    html = sdk.HTMLResponse("<h1>Hello</h1>")
    assert html.body == b"<h1>Hello</h1>"
    assert html.headers["content-type"] == "text/html; charset=utf-8"

    data = sdk.JSONResponse({"message": "olá", "count": 2})
    assert data.body == '{"message":"olá","count":2}'.encode()
    assert data.headers["content-type"] == "application/json; charset=utf-8"

    scalar = sdk.JSONResponse(None)
    assert scalar.status == 200
    assert scalar.body == b"null"


def test_explicit_response_content_type_can_be_overridden() -> None:
    html = sdk.HTMLResponse("<feed />", headers=[("Content-Type", "application/xhtml+xml")])
    data = sdk.JSONResponse({}, headers=[("content-type", "application/problem+json")])

    assert html.headers.get_all("content-type") == ["application/xhtml+xml"]
    assert data.headers.get_all("content-type") == ["application/problem+json"]

    with pytest.raises(TypeError, match="must be a string"):
        sdk.HTMLResponse(b"<h1>Hello</h1>")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        sdk.JSONResponse(float("nan"))


def test_sdk_and_installed_package_import_only_the_standard_library() -> None:
    """Everything shipped into sandboxes runs without Hatchery's dependencies."""
    installed = runtime.sandbox_files()
    python_files = {path: content for path, content in installed.items() if path.endswith(".py")}
    assert set(python_files) == {
        "hatchery/__init__.py",
        "hatchery/sdk/__init__.py",
        "hatchery/sdk/__main__.py",
    }
    assert python_files["hatchery/sdk/__main__.py"] == (SDK / "__main__.py").read_bytes()
    imported: set[str] = set()
    for content in python_files.values():
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module
                imported.add(node.module.partition(".")[0])
    assert imported - {"hatchery"} <= sys.stdlib_module_names


def test_sdk_runs_from_only_the_installed_runtime_files(tmp_path: pathlib.Path) -> None:
    """The sandbox layout: the runtime directory alone provides `hatchery.sdk`."""
    runtime_dir = tmp_path / "runtime"
    for path, content in runtime.sandbox_files().items():
        (runtime_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (runtime_dir / path).write_bytes(content)
    handler = tmp_path / "self" / "api" / "route.py"
    handler.parent.mkdir(parents=True)
    handler.write_text("from hatchery.sdk import Response\ndef POST(request):\n    return 'ok'\n")

    result = subprocess.run(
        [sys.executable, "-S", "-m", "hatchery.sdk"],
        input=json.dumps(
            {
                "version": 1,
                "kind": "request",
                "request_id": "r",
                "handler": str(handler),
                "method": "POST",
                "path": "/api",
                "params": {},
                "query": [],
                "headers": [],
                "body_b64": "",
            }
        ),
        capture_output=True,
        text=True,
        cwd=runtime_dir,
        env={"PYTHONPATH": str(tmp_path / "self")},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert base64.b64decode(json.loads(result.stdout)["body_b64"]) == b"ok"
