"""Send AI SDK traces to Braintrust."""

import contextlib
import json
import os
import typing

import ai.experimental_telemetry
import ai.experimental_telemetry.otel

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
        candidate.set_attrs(
            {
                "braintrust.input_json": json.dumps({"chat_id": chat.id}),
                "braintrust.span_attributes": json.dumps({"type": "task"}),
                "chat.id": chat.id,
            },
            trigger=chat.trigger,
        )
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
