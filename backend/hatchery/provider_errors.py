"""Narrow provider failure normalization until the AI SDK exposes semantic limit errors.

Ported from agentmesh `provider_errors.py`.
"""

import enum
import typing

import ai.errors


class RequestFailure(enum.Enum):
    CONTEXT_OVERFLOW = "context_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    OTHER = "other"


CONTEXT_CODES = frozenset({"context_length_exceeded", "context_window_exceeded"})


def classify_request_failure(error: ai.errors.ProviderAPIError) -> RequestFailure:
    if isinstance(error, ai.errors.ProviderRequestTooLargeError):
        return RequestFailure.PAYLOAD_TOO_LARGE
    body: typing.Any = error.body
    codes: set[typing.Any] = {error.code, error.type}
    if isinstance(body, dict):
        codes.update((body.get("code"), body.get("type")))
        nested = body.get("error")
        if isinstance(nested, dict):
            codes.update((nested.get("code"), nested.get("type")))
    if {code.lower() for code in codes if isinstance(code, str)} & CONTEXT_CODES:
        return RequestFailure.CONTEXT_OVERFLOW
    return RequestFailure.OTHER
