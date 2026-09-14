"""Send AI SDK traces to Traces over authenticated OTLP/HTTP."""

import contextlib
import os

import ai.experimental_telemetry
import ai.experimental_telemetry.otel
import opentelemetry.exporter.otlp.proto.http
import opentelemetry.exporter.otlp.proto.http.trace_exporter
import opentelemetry.sdk.resources
import opentelemetry.sdk.trace
import opentelemetry.sdk.trace.export
import requests
import vercel.oidc

_adapter: ai.experimental_telemetry.otel.OtelAdapter | None = None


@contextlib.asynccontextmanager
async def use_chat(chat_id: str):
    """Attach work to one durable trace shared by every turn in a chat."""
    if not ai.experimental_telemetry.is_enabled():
        yield None
        return

    from store import chats

    chat = await chats.get(chat_id)
    if chat is None:
        raise ValueError(f"unknown chat {chat_id}")
    root = (
        ai.experimental_telemetry.Span[
            ai.experimental_telemetry.CustomSpanData
        ].model_validate(chat.telemetry_span)
        if chat.telemetry_span
        else None
    )
    if root is None:
        candidate = ai.experimental_telemetry.create_span("hatchery.chat").stamp_start()
        candidate.set_attrs({"chat.id": chat.id}, trigger=chat.trigger)
        candidate.stamp_end()
        if candidate.id:
            saved = await chats.set_telemetry_span_if_absent(
                chat.id, candidate.model_dump(mode="json")
            )
            if saved is None or saved.telemetry_span is None:
                raise ValueError(f"unknown chat {chat_id}")
            root = ai.experimental_telemetry.Span[
                ai.experimental_telemetry.CustomSpanData
            ].model_validate(saved.telemetry_span)
            if root.id == candidate.id:
                await candidate.push()
    current = ai.experimental_telemetry.current_span()
    if root is None or (current is not None and current.trace_id == root.trace_id):
        yield root
        return
    async with ai.experimental_telemetry.use_span(root):
        try:
            yield root
        finally:
            flush()


class _TracesSession(requests.Session):
    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        # Resolve on every send, not at module import: function tokens rotate.
        request.headers["x-vercel-trusted-oidc-idp-token"] = (
            vercel.oidc.get_vercel_oidc_token()
        )
        # A protection login redirect is not an accepted export. Never forward
        # the OIDC header to a redirect target or let OTLP count it as success.
        kwargs["allow_redirects"] = False
        response = super().send(request, **kwargs)
        if 300 <= response.status_code < 400:
            response.close()
            raise requests.exceptions.HTTPError(
                "Traces redirected; check deployment protection"
            )
        return response


def install() -> ai.experimental_telemetry.otel.OtelAdapter | None:
    """Enable Traces on Vercel, or locally with an explicit TRACES_URL."""
    global _adapter
    if _adapter is not None:
        return _adapter

    url = os.environ.get(
        "TRACES_URL",
        ("https://traces.playground-vercel.tools" if os.environ.get("VERCEL") else ""),
    )
    if not url:
        return None

    provider = opentelemetry.sdk.trace.TracerProvider(
        resource=opentelemetry.sdk.resources.Resource.create(
            {
                "service.name": "hatchery",
                "vercel.deployment.id": os.environ.get("VERCEL_DEPLOYMENT_ID", "local"),
                "deployment.environment.name": os.environ.get(
                    "VERCEL_ENV", "development"
                ),
                "vcs.ref.head.revision": os.environ.get("VERCEL_GIT_COMMIT_SHA", ""),
            }
        )
    )
    exporter = opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter(
        endpoint=f"{url.rstrip('/')}/v1/traces",
        session=_TracesSession(),
        timeout=5,
        compression=opentelemetry.exporter.otlp.proto.http.Compression.NoCompression,
    )
    # Export in the invocation's context, where Vercel's rotating OIDC header is
    # available. A background BatchSpanProcessor thread loses that context.
    provider.add_span_processor(
        opentelemetry.sdk.trace.export.SimpleSpanProcessor(exporter)
    )
    _adapter = ai.experimental_telemetry.otel.OtelAdapter(
        tracer_provider=provider, capture_content=True
    )
    ai.experimental_telemetry.register(_adapter)
    return _adapter


def flush() -> None:
    """Flush pending spans before the serverless invocation can be frozen."""
    if _adapter is not None:
        _adapter.flush()
