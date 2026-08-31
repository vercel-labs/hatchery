"""Send AI SDK traces to Braintrust."""

import os
import typing

import ai.experimental_telemetry
import ai.experimental_telemetry.otel

_adapter: ai.experimental_telemetry.otel.OtelAdapter | None = None


class _BraintrustAdapter(ai.experimental_telemetry.otel.OtelAdapter):
    def span_attrs(
        self, span: ai.experimental_telemetry.Span, /
    ) -> dict[str, typing.Any]:
        return super().span_attrs(span) | {
            "braintrust.metadata.vercel_deployment_id": os.environ.get(
                "VERCEL_DEPLOYMENT_ID", "local"
            ),
            "braintrust.metadata.vercel_environment": os.environ.get(
                "VERCEL_ENV", "development"
            ),
            "braintrust.metadata.git_commit_sha": os.environ.get(
                "VERCEL_GIT_COMMIT_SHA", ""
            ),
        }


def install() -> ai.experimental_telemetry.otel.OtelAdapter | None:
    """Install Braintrust tracing when its API key and project are configured."""
    global _adapter
    if _adapter is not None:
        return _adapter

    api_key = os.environ.get("BRAINTRUST_API_KEY")
    parent = os.environ.get("BRAINTRUST_PARENT")
    if not parent and (project_id := os.environ.get("BRAINTRUST_PROJECT_ID")):
        parent = f"project_id:{project_id}"
    if not api_key or not parent:
        return None

    import braintrust.otel
    import opentelemetry.sdk.resources
    import opentelemetry.sdk.trace

    provider = opentelemetry.sdk.trace.TracerProvider(
        resource=opentelemetry.sdk.resources.Resource.create(
            {"service.name": "hatchery"}
        )
    )
    provider.add_span_processor(
        braintrust.otel.BraintrustSpanProcessor(api_key=api_key, parent=parent)
    )
    _adapter = _BraintrustAdapter(tracer_provider=provider, capture_content=True)
    ai.experimental_telemetry.register(_adapter)
    return _adapter


def flush() -> None:
    """Flush pending spans before the serverless invocation can be frozen."""
    if _adapter is not None:
        _adapter.flush()
