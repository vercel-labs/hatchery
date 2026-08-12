"""Install Braintrust tracing for the worker process."""

import os
import typing

import ai.experimental_telemetry
import ai.experimental_telemetry.otel as otel_adapter

_adapter: otel_adapter.OtelAdapter | None = None


class _BraintrustAdapter(otel_adapter.OtelAdapter):
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


def install() -> otel_adapter.OtelAdapter | None:
    """Install Braintrust when its API key and project are configured."""
    global _adapter
    if _adapter is not None:
        return _adapter

    api_key = os.environ.get("BRAINTRUST_API_KEY")
    project_id = os.environ.get("BRAINTRUST_PROJECT_ID")
    if not api_key or not project_id:
        return None

    import braintrust.otel
    import opentelemetry.sdk.resources
    import opentelemetry.sdk.trace

    provider = opentelemetry.sdk.trace.TracerProvider(
        resource=opentelemetry.sdk.resources.Resource.create(
            {"service.name": "fabricator"}
        )
    )
    provider.add_span_processor(
        braintrust.otel.BraintrustSpanProcessor(
            api_key=api_key,
            parent=f"project_id:{project_id}",
        )
    )
    adapter = _BraintrustAdapter(tracer_provider=provider, capture_content=True)
    ai.experimental_telemetry.register(adapter)
    _adapter = adapter
    return adapter


def flush() -> None:
    """Flush the installed exporter, if any."""
    if _adapter is not None:
        _adapter.flush()
