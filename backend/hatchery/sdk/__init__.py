"""Standard-library types for Hatchery HTTP handlers and scheduled jobs.

Ported from agentmesh `sdk/`. This package is installed read-only into serve and
thread sandboxes, so it imports only the standard library and must keep working on
the sandbox's Python (3.11+).
"""

import collections.abc
import contextvars
import hashlib
import json
import math
import operator
from typing import Any, Literal, NotRequired, SupportsIndex, TypedDict, overload

__all__ = [
    "HTMLResponse",
    "Headers",
    "JSONResponse",
    "Job",
    "PromptEffect",
    "Query",
    "Request",
    "Response",
    "prompt",
]

_MAX_PROMPT_TEXT = 100_000
_MAX_EFFECT_VALUE = 200


class Query(list[tuple[str, str]]):
    """Ordered query pairs, including repeated names."""

    def __init__(self, pairs: collections.abc.Iterable[tuple[str, str]] = ()) -> None:
        super().__init__(_pairs(pairs, "query"))

    def __iter__(self) -> collections.abc.Iterator[tuple[str, str]]:
        return super().__iter__()

    @overload
    def __getitem__(self, index: SupportsIndex) -> tuple[str, str]: ...

    @overload
    def __getitem__(self, index: slice) -> list[tuple[str, str]]: ...

    @overload
    def __getitem__(self, index: str) -> str: ...

    def __getitem__(
        self, index: SupportsIndex | slice | str
    ) -> tuple[str, str] | list[tuple[str, str]] | str:
        if isinstance(index, str):
            value = self.get(index)
            if value is None:
                raise KeyError(index)
            return value
        if isinstance(index, slice):
            return super().__getitem__(index)
        return super().__getitem__(operator.index(index))

    def append(self, pair: tuple[str, str]) -> None:
        super().extend(_pairs((pair,), "query"))

    def get(self, name: str, default: str | None = None) -> str | None:
        """Return the first value for a name."""
        for candidate, value in self:
            if candidate == name:
                return value
        return default

    def get_all(self, name: str) -> list[str]:
        """Return every value for a name in wire order."""
        return [value for candidate, value in self if candidate == name]


class Headers(Query):
    """Ordered header pairs with case-insensitive first-value lookup."""

    def __init__(self, pairs: collections.abc.Iterable[tuple[str, str]] = ()) -> None:
        list.__init__(self, _pairs(pairs, "headers", require_name=True))

    def append(self, pair: tuple[str, str]) -> None:
        super().extend(_pairs((pair,), "headers", require_name=True))

    def get(self, name: str, default: str | None = None) -> str | None:
        folded = name.casefold()
        for candidate, value in self:
            if candidate.casefold() == folded:
                return value
        return default

    def get_all(self, name: str) -> list[str]:
        folded = name.casefold()
        return [value for candidate, value in self if candidate.casefold() == folded]


class Request:
    """A transport-neutral HTTP request passed to a handler."""

    def __init__(
        self,
        id: str,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        query: collections.abc.Iterable[tuple[str, str]] = (),
        headers: collections.abc.Iterable[tuple[str, str]] = (),
        body: bytes = b"",
    ) -> None:
        self.id = _bounded_string(id, "id", _MAX_EFFECT_VALUE)
        self.method = _bounded_string(method, "method", 32)
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must be a string starting with '/'")
        if params is None:
            params = {}
        if type(params) is not dict or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in params.items()
        ):
            raise TypeError("params must be a dict of strings")
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")

        self.path = path
        self.params = dict(params)
        self.query = Query(query)
        self.headers = Headers(headers)
        self.body = body

    def json(self) -> Any:
        """Decode the request body as UTF-8 JSON."""
        return json.loads(self.body)


class Job:
    """One scheduled job occurrence passed to ``run``."""

    def __init__(self, id: str, name: str, scheduled_for: float, revision: str) -> None:
        self.id = _bounded_string(id, "id", _MAX_EFFECT_VALUE)
        self.name = _bounded_string(name, "name", _MAX_EFFECT_VALUE)
        if isinstance(scheduled_for, bool) or not isinstance(scheduled_for, (int, float)):
            raise TypeError("scheduled_for must be a Unix timestamp")
        if not math.isfinite(scheduled_for):
            raise ValueError("scheduled_for must be finite")
        self.scheduled_for = float(scheduled_for)
        self.revision = _bounded_string(revision, "revision", _MAX_EFFECT_VALUE)


class Response:
    """An HTTP response with normalized bytes and repeated headers."""

    def __init__(
        self,
        body: dict[str, Any] | list[Any] | str | bytes | None = None,
        *,
        status: int | None = None,
        headers: collections.abc.Iterable[tuple[str, str]] = (),
    ) -> None:
        if status is None:
            status = 204 if body is None else 200
        if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
            raise ValueError("status must be an integer from 200 through 599")

        normalized_headers = Headers(headers)
        content_type: str | None
        if body is None:
            normalized_body = b""
            content_type = None
        elif isinstance(body, bytes):
            normalized_body = body
            content_type = "application/octet-stream"
        elif isinstance(body, str):
            normalized_body = body.encode()
            content_type = "text/plain; charset=utf-8"
        elif type(body) in (dict, list):
            normalized_body = json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            content_type = "application/json; charset=utf-8"
        else:
            raise TypeError("response body must be a dict, list, str, bytes, or None")

        if content_type is not None and normalized_headers.get("content-type") is None:
            normalized_headers.append(("Content-Type", content_type))
        self.status = status
        self.headers = normalized_headers
        self.body = normalized_body


class HTMLResponse(Response):
    """An HTML response with a UTF-8 content type by default."""

    def __init__(
        self,
        body: str,
        *,
        status: int | None = None,
        headers: collections.abc.Iterable[tuple[str, str]] = (),
    ) -> None:
        if not isinstance(body, str):
            raise TypeError("HTML response body must be a string")
        normalized_headers = Headers(headers)
        if normalized_headers.get("content-type") is None:
            normalized_headers.append(("Content-Type", "text/html; charset=utf-8"))
        super().__init__(body, status=status, headers=normalized_headers)


class JSONResponse(Response):
    """A response that serializes any JSON-compatible value."""

    def __init__(
        self,
        body: Any,
        *,
        status: int | None = None,
        headers: collections.abc.Iterable[tuple[str, str]] = (),
    ) -> None:
        normalized_body = json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        normalized_headers = Headers(headers)
        if normalized_headers.get("content-type") is None:
            normalized_headers.append(("Content-Type", "application/json; charset=utf-8"))
        super().__init__(normalized_body, status=status, headers=normalized_headers)


class PromptEffect(TypedDict):
    """A serialized request to prompt this agent."""

    type: Literal["prompt"]
    text: str
    key: str
    thread: NotRequired[str]


_effects: contextvars.ContextVar[list[PromptEffect] | None] = contextvars.ContextVar(
    "hatchery_sdk_effects", default=None
)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hatchery_sdk_request_id", default=None
)
_request_path: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hatchery_sdk_request_path", default=None
)


def prompt(text: str, *, key: str | None = None, thread: str | None = None) -> None:
    """Record one bounded prompt effect for this invocation without doing I/O.

    Text is limited to 100,000 characters; key and thread are limited to 200.
    Every supplied value must contain non-whitespace text.
    """
    normalized_text = _bounded_string(text, "text", _MAX_PROMPT_TEXT)
    normalized_key = None if key is None else _bounded_string(key, "key", _MAX_EFFECT_VALUE)
    normalized_thread = (
        None if thread is None else _bounded_string(thread, "thread", _MAX_EFFECT_VALUE)
    )
    collector = _effects.get()
    if collector is None:
        raise RuntimeError("prompt() is only available while a handler is running")
    if key is None:
        request_id = _request_id.get()
        if request_id is None:
            raise RuntimeError("prompt() has no active request")
        candidate = f"{request_id}:{len(collector)}"
        normalized_key = (
            candidate
            if len(candidate) <= _MAX_EFFECT_VALUE
            else hashlib.sha256(candidate.encode()).hexdigest()
        )
    if normalized_thread is None:
        normalized_thread = _request_path.get()
    assert normalized_key is not None

    effect: PromptEffect = {"type": "prompt", "text": normalized_text, "key": normalized_key}
    if normalized_thread is not None:
        effect["thread"] = normalized_thread
    collector.append(effect)


def _bounded_string(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def _pairs(
    pairs: collections.abc.Iterable[tuple[str, str]], name: str, *, require_name: bool = False
) -> list[tuple[str, str]]:
    try:
        normalized = list(pairs)
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable of string pairs") from exc
    if any(
        not isinstance(pair, (tuple, list))
        or len(pair) != 2
        or not isinstance(pair[0], str)
        or not isinstance(pair[1], str)
        or (require_name and not pair[0])
        for pair in normalized
    ):
        raise TypeError(f"{name} must contain string pairs")
    return [(pair[0], pair[1]) for pair in normalized]
