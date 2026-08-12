from unittest import mock

import ai.experimental_telemetry
import braintrust.otel
import opentelemetry.sdk.resources
import opentelemetry.sdk.trace

from agent import telemetry


def test_install_is_disabled_without_config(monkeypatch):
    monkeypatch.setattr(telemetry, "_adapter", None)
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT_ID", raising=False)

    assert telemetry.install() is None


def test_install_configures_and_registers_adapter(monkeypatch):
    provider = mock.Mock()
    processor = mock.sentinel.processor
    adapter = mock.sentinel.adapter
    resource = mock.sentinel.resource
    monkeypatch.setattr(telemetry, "_adapter", None)
    monkeypatch.setenv("BRAINTRUST_API_KEY", "api-key")
    monkeypatch.setenv("BRAINTRUST_PROJECT_ID", "project-1")
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
        {"service.name": "fabricator"}
    )
    opentelemetry.sdk.trace.TracerProvider.assert_called_once_with(
        resource=resource
    )
    processor_factory.assert_called_once_with(
        api_key="api-key", parent="project_id:project-1"
    )
    provider.add_span_processor.assert_called_once_with(processor)
    adapter_factory.assert_called_once_with(
        tracer_provider=provider, capture_content=True
    )
    register.assert_called_once_with(adapter)


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
