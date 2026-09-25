"""Execute one Hatchery SDK handler request or scheduled job over standard streams.

Ported from agentmesh `sdk/__main__.py`. Reads one strict versioned JSON envelope,
imports one explicit handler file, and writes one response envelope plus bounded
effects. Handler stdout goes to stderr as diagnostics.
"""

import asyncio
import base64
import collections.abc
import contextlib
import importlib.util
import inspect
import json
import os
import pathlib
import sys
import traceback
import types
import uuid
from typing import Any, TextIO

from hatchery import sdk

_REQUEST_FIELDS = {
    "version",
    "kind",
    "request_id",
    "handler",
    "method",
    "path",
    "params",
    "query",
    "headers",
    "body_b64",
}
_JOB_FIELDS = {
    "version",
    "kind",
    "run_id",
    "handler",
    "name",
    "scheduled_for",
    "revision",
}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def main(
    stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> int:
    """Read, execute, and write one request; return a process-style exit code."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    try:
        envelope = _read_envelope(stdin)
        subject = _subject(envelope)
    except Exception as exc:
        print(f"invalid request envelope: {exc}", file=stderr)
        return 2

    previous = pathlib.Path.cwd()
    try:
        data = os.environ.get("HATCHERY_DATA")
        if data is not None:
            target = pathlib.Path(data)
            if not target.is_absolute() or not target.is_dir():
                raise ValueError("HATCHERY_DATA must be an existing absolute directory")
            os.chdir(target)
        try:
            with contextlib.redirect_stdout(stderr):
                module = _load_module(envelope["handler"])
        except Exception:
            print("handler import failed", file=stderr)
            traceback.print_exc(file=stderr)
            return 2

        effects: list[sdk.PromptEffect] = []
        try:
            response = _invoke(module, subject, effects, stderr)
        finally:
            sys.modules.pop(module.__name__, None)
    finally:
        os.chdir(previous)

    output = {
        "version": 1,
        "status": response.status,
        "headers": list(response.headers),
        "body_b64": base64.b64encode(response.body).decode("ascii"),
        "effects": effects,
    }
    json.dump(output, stdout, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    stdout.write("\n")
    stdout.flush()
    return 0


def _read_envelope(stdin: TextIO) -> dict[str, Any]:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("JSON objects must not contain duplicate names")
            result[name] = value
        return result

    value = json.loads(
        stdin.read(),
        object_pairs_hook=object_from_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON number")),
    )
    if type(value) is not dict:
        raise ValueError("request must be an object")
    fields = _REQUEST_FIELDS if value.get("kind") == "request" else _JOB_FIELDS
    if value.get("kind") not in ("request", "job") or set(value) != fields:
        raise ValueError("request must contain exactly the documented fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("version must be 1")
    if not isinstance(value["handler"], str) or not pathlib.Path(value["handler"]).is_absolute():
        raise ValueError("handler must be an absolute path")
    if value["kind"] == "request":
        if type(value["params"]) is not dict or any(
            not isinstance(name, str) or not isinstance(item, str)
            for name, item in value["params"].items()
        ):
            raise TypeError("params must be an object of strings")
        _validate_pairs(value["query"], "query")
        _validate_pairs(value["headers"], "headers", require_name=True)
        if not isinstance(value["body_b64"], str):
            raise TypeError("body_b64 must be a string")
    return value


def _subject(envelope: collections.abc.Mapping[str, Any]) -> sdk.Request | sdk.Job:
    if envelope["kind"] == "job":
        return sdk.Job(
            envelope["run_id"],
            envelope["name"],
            envelope["scheduled_for"],
            envelope["revision"],
        )
    try:
        body = base64.b64decode(envelope["body_b64"].encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("body_b64 must be canonical base64") from exc
    if base64.b64encode(body).decode("ascii") != envelope["body_b64"]:
        raise ValueError("body_b64 must be canonical base64")
    return sdk.Request(
        envelope["request_id"],
        envelope["method"],
        envelope["path"],
        params=envelope["params"],
        query=envelope["query"],
        headers=envelope["headers"],
        body=body,
    )


def _validate_pairs(value: object, name: str, *, require_name: bool = False) -> None:
    if not isinstance(value, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not isinstance(pair[0], str)
        or not isinstance(pair[1], str)
        or (require_name and not pair[0])
        for pair in value
    ):
        raise TypeError(f"{name} must be an array of string pairs")


def _load_module(handler: str) -> types.ModuleType:
    name = f"_hatchery_handler_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, handler)
    if spec is None or spec.loader is None:
        raise ImportError("handler path is not a loadable Python file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _invoke(
    module: types.ModuleType,
    subject: sdk.Request | sdk.Job,
    effects: list[sdk.PromptEffect],
    stderr: TextIO,
) -> sdk.Response:
    function = subject.method if isinstance(subject, sdk.Request) else "run"
    if (isinstance(subject, sdk.Request) and subject.method not in _METHODS) or not hasattr(
        module, function
    ):
        return sdk.Response(None, status=405)

    token = sdk._effects.set(effects)
    request_token = sdk._request_id.set(subject.id)
    path_token = sdk._request_path.set(
        subject.path if isinstance(subject, sdk.Request) else f"schedule:{subject.name}"
    )
    try:
        with contextlib.redirect_stdout(stderr):
            value = getattr(module, function)(subject)
            if inspect.isawaitable(value):
                value = asyncio.run(_await(value))
        return value if isinstance(value, sdk.Response) else sdk.Response(value)
    except BaseException:
        effects.clear()
        traceback.print_exc(file=stderr)
        return sdk.Response("Internal Server Error", status=500)
    finally:
        sdk._request_path.reset(path_token)
        sdk._request_id.reset(request_token)
        sdk._effects.reset(token)


async def _await(value: collections.abc.Awaitable[Any]) -> Any:
    return await value


if __name__ == "__main__":
    raise SystemExit(main())
