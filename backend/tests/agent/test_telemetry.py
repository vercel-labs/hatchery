from unittest import mock

import ai.experimental_telemetry
import braintrust.otel
import opentelemetry.sdk.resources
import opentelemetry.sdk.trace

from agent import telemetry
from store import chats


async def test_use_chat_persists_and_reuses_one_trace():
    chat = await chats.create(None, "trace me")
    sink = ai.experimental_telemetry.DictSink()

    async with ai.experimental_telemetry.use_sink(sink):
        async with telemetry.use_chat(chat.id) as first:
            async with ai.experimental_telemetry.span("first"):
                pass
        async with telemetry.use_chat(chat.id) as second:
            async with ai.experimental_telemetry.span("second"):
                pass

    saved = await chats.get(chat.id)
    assert saved is not None and saved.telemetry_span is not None
    assert first is not None and second is not None
    assert first.id == second.id
    assert first.trace_id == second.trace_id
    assert [span.name for span in sink.finished_spans].count("hatchery.chat") == 1
    children = [span for span in sink.finished_spans if span.name in {"first", "second"}]
    assert {span.trace_id for span in children} == {first.trace_id}
    assert {span.parent_id for span in children} == {first.id}


async def test_nested_use_chat_preserves_the_active_child():
    chat = await chats.create(None, "nested")
    sink = ai.experimental_telemetry.DictSink()

    async with ai.experimental_telemetry.use_sink(sink):
        async with telemetry.use_chat(chat.id):
            async with ai.experimental_telemetry.span("parent") as parent:
                async with telemetry.use_chat(chat.id):
                    async with ai.experimental_telemetry.span("child"):
                        pass

    child = next(span for span in sink.finished_spans if span.name == "child")
    assert child.parent_id == parent.id
    assert child.trace_id == parent.trace_id


def test_install_is_disabled_without_config(monkeypatch):
    monkeypatch.setattr(telemetry, "_adapter", None)
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_PARENT", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT_ID", raising=False)

    assert telemetry.install() is None


def test_install_configures_and_registers_adapter(monkeypatch):
    provider = mock.Mock()
    processor = mock.sentinel.processor
    adapter = mock.sentinel.adapter
    resource = mock.sentinel.resource
    monkeypatch.setattr(telemetry, "_adapter", None)
    monkeypatch.setenv("BRAINTRUST_API_KEY", "api-key")
    monkeypatch.setenv("BRAINTRUST_PARENT", "project_name:hatchery")
    monkeypatch.setattr(
        opentelemetry.sdk.resources.Resource,
        "create",
        mock.Mock(return_value=resource),
    )
    monkeypatch.setattr(
        opentelemetry.sdk.trace,
        "TracerProvider",
        mock.Mock(return_value=provider),
    )
    processor_factory = mock.Mock(return_value=processor)
    monkeypatch.setattr(
        braintrust.otel, "BraintrustSpanProcessor", processor_factory
    )
    adapter_factory = mock.Mock(return_value=adapter)
    monkeypatch.setattr(telemetry, "_BraintrustAdapter", adapter_factory)
    register = mock.Mock()
    monkeypatch.setattr(ai.experimental_telemetry, "register", register)

    assert telemetry.install() is adapter
    opentelemetry.sdk.resources.Resource.create.assert_called_once_with(
        {"service.name": "hatchery"}
    )
    opentelemetry.sdk.trace.TracerProvider.assert_called_once_with(resource=resource)
    processor_factory.assert_called_once_with(
        api_key="api-key", parent="project_name:hatchery"
    )
    provider.add_span_processor.assert_called_once_with(processor)
    adapter_factory.assert_called_once_with(
        tracer_provider=provider, capture_content=True
    )
    register.assert_called_once_with(adapter)


def test_install_accepts_project_id(monkeypatch):
    monkeypatch.setattr(telemetry, "_adapter", None)
    monkeypatch.setenv("BRAINTRUST_API_KEY", "api-key")
    monkeypatch.delenv("BRAINTRUST_PARENT", raising=False)
    monkeypatch.setenv("BRAINTRUST_PROJECT_ID", "project-1")
    processor = mock.patch(
        "braintrust.otel.BraintrustSpanProcessor", return_value=mock.Mock()
    )

    with processor as processor_factory:
        telemetry.install()

    processor_factory.assert_called_once_with(
        api_key="api-key", parent="project_id:project-1"
    )


def test_adapter_adds_deployment_metadata(monkeypatch):
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "deployment-1")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc123")
    adapter = telemetry._BraintrustAdapter(
        tracer_provider=opentelemetry.sdk.trace.TracerProvider()
    )
    span = ai.experimental_telemetry.Span(
        name="work",
        data=ai.experimental_telemetry.CustomSpanData(attrs={}),
        id="span-1",
        trace_id="trace-1",
    )

    attributes = adapter.span_attrs(span)

    assert attributes["braintrust.metadata.vercel_deployment_id"] == "deployment-1"
    assert attributes["braintrust.metadata.vercel_environment"] == "preview"
    assert attributes["braintrust.metadata.git_commit_sha"] == "abc123"


def test_flush_uses_installed_adapter(monkeypatch):
    adapter = mock.Mock()
    monkeypatch.setattr(telemetry, "_adapter", adapter)

    telemetry.flush()

    adapter.flush.assert_called_once_with()
