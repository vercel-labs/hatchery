import ai.errors

from hatchery import provider_errors


def test_context_overflow_detection_is_narrow_for_provider_bad_requests():
    classify = provider_errors.classify_request_failure
    failure = provider_errors.RequestFailure
    assert (
        classify(ai.errors.ProviderRequestTooLargeError("payload rejected"))
        is failure.PAYLOAD_TOO_LARGE
    )
    assert (
        classify(ai.errors.ProviderBadRequestError("invalid", code="context_length_exceeded"))
        is failure.CONTEXT_OVERFLOW
    )
    assert (
        classify(
            ai.errors.ProviderBadRequestError(
                "generic message is ignored", body={"error": {"code": "context_window_exceeded"}}
            )
        )
        is failure.CONTEXT_OVERFLOW
    )
    assert (
        classify(ai.errors.ProviderBadRequestError("context length exceeded in prose"))
        is failure.OTHER
    )
