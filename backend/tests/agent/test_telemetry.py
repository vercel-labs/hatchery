import hashlib
import http.server
import json
import threading
import time

import ai
import ai.experimental_telemetry
import ai.testing
import jwt
import opentelemetry.proto.collector.trace.v1.trace_service_pb2
import pytest
import vercel.headers
import vercel.oidc.token

from agent import telemetry
from store import chats


async def test_use_chat_persists_and_reuses_one_trace():
    chat = await chats.create(None, "trace me")
    sink = ai.experimental_telemetry.DictSink()

    async with ai.experimental_telemetry.use_sink(sink):
        async with (
            telemetry.use_chat(chat.id) as first,
            ai.experimental_telemetry.span("first"),
        ):
            pass
        async with (
            telemetry.use_chat(chat.id) as second,
            ai.experimental_telemetry.span("second"),
        ):
            pass

    saved = await chats.get(chat.id)
    assert saved is not None and saved.telemetry_span is not None
    assert first is not None and second is not None
    assert first.id == second.id
    assert first.trace_id == second.trace_id
    assert [span.name for span in sink.finished_spans].count("hatchery.chat") == 1
    children = [
        span for span in sink.finished_spans if span.name in {"first", "second"}
    ]
    assert {span.trace_id for span in children} == {first.trace_id}
    assert {span.parent_id for span in children} == {first.id}


async def test_nested_use_chat_preserves_the_active_child():
    chat = await chats.create(None, "nested")
    sink = ai.experimental_telemetry.DictSink()

    async with (
        ai.experimental_telemetry.use_sink(sink),
        telemetry.use_chat(chat.id),
        ai.experimental_telemetry.span("parent") as parent,
        telemetry.use_chat(chat.id),
        ai.experimental_telemetry.span("child"),
    ):
        pass

    child = next(span for span in sink.finished_spans if span.name == "child")
    assert child.parent_id == parent.id
    assert child.trace_id == parent.trace_id


@pytest.fixture
def collector(monkeypatch):
    received = []
    status = [200]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, dict(self.headers), body))
            self.send_response(status[0])
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            if status[0] == 302:
                self.send_header("Location", "/login")
            self.end_headers()

        def do_GET(self):
            received.append((self.path, dict(self.headers), b""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(telemetry, "_adapter", None)
    monkeypatch.setattr(vercel.oidc.token, "_cached_oidc_token", None)
    monkeypatch.setattr(vercel.oidc.token, "_cached_oidc_payload", None)
    monkeypatch.setenv("TRACES_URL", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "deployment-test")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc123")
    try:
        yield received, status
    finally:
        if telemetry._adapter is not None:
            ai.experimental_telemetry.unregister(telemetry._adapter)
            telemetry._adapter.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("vercel", [False, True])
def test_install_can_be_disabled(monkeypatch, vercel):
    monkeypatch.setattr(telemetry, "_adapter", None)
    if vercel:
        monkeypatch.setenv("VERCEL", "1")
        monkeypatch.setenv("TRACES_URL", "")
    else:
        monkeypatch.delenv("VERCEL", raising=False)
        monkeypatch.delenv("TRACES_URL", raising=False)
    assert telemetry.install() is None
    telemetry.flush()


async def test_export_content_parentage_and_rotating_request_tokens(
    collector, monkeypatch
):
    received, _ = collector
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "stale-build-token")
    adapter = telemetry.install()
    assert adapter is not None
    assert telemetry.install() is adapter
    chat = await chats.create(None, "trace me")
    tokens = [
        jwt.encode({"exp": time.time() + 3600 + i}, "", algorithm="none")
        for i in range(2)
    ]
    for i, token in enumerate(tokens):
        with vercel.headers.HeadersContext({"x-vercel-oidc-token": token}).use():
            async with telemetry.use_chat(chat.id):
                if i == 0:
                    model = ai.testing.FakeModel(
                        [ai.user_message("hi"), ai.assistant_message("hello")]
                    )
                    async with ai.stream(model, [ai.user_message("hi")]) as stream:
                        async for _ in stream:
                            pass
                else:
                    with pytest.raises(ValueError, match="test failure"):
                        async with ai.experimental_telemetry.span("second turn"):
                            raise ValueError("test failure")
            telemetry.flush()
    assert received
    assert {
        headers["x-vercel-trusted-oidc-idp-token"] for _, headers, _ in received
    } == set(tokens)
    spans = []
    for path, headers, body in received:
        assert path == "/v1/traces"
        assert headers["Content-Type"] == "application/x-protobuf"
        export = opentelemetry.proto.collector.trace.v1.trace_service_pb2.ExportTraceServiceRequest.FromString(
            body
        )
        for resource in export.resource_spans:
            attrs = {
                item.key: item.value.string_value
                for item in resource.resource.attributes
            }
            assert attrs["service.name"] == "hatchery"
            assert attrs["vercel.deployment.id"] == "deployment-test"
            assert attrs["deployment.environment.name"] == "preview"
            assert attrs["vcs.ref.head.revision"] == "abc123"
            for scope in resource.scope_spans:
                spans.extend(scope.spans)
    root = next(span for span in spans if span.name == "hatchery.chat")
    saved = await chats.get(chat.id)
    assert (
        root.trace_id
        == hashlib.sha256(saved.telemetry_span["trace_id"].encode()).digest()[:16]
    )
    assert all(span.trace_id == root.trace_id for span in spans)
    assert all(
        span.parent_span_id == root.span_id for span in spans if span is not root
    )
    model = next(span for span in spans if span.name.startswith("chat "))
    attrs = {item.key: item.value.string_value for item in model.attributes}
    assert json.loads(attrs["gen_ai.input.messages"])[0]["parts"][0]["content"] == "hi"
    assert (
        json.loads(attrs["gen_ai.output.messages"])[0]["parts"][0]["content"] == "hello"
    )
    assert next(span for span in spans if span.name == "second turn").status.code == 2


@pytest.mark.parametrize("status_code", [302, 401, 403, 404])
async def test_rejected_export_is_logged_without_breaking_agent(
    collector, monkeypatch, caplog, status_code
):
    received, status = collector
    status[0] = status_code
    monkeypatch.setenv(
        "VERCEL_OIDC_TOKEN",
        jwt.encode({"exp": time.time() + 3600}, "", algorithm="none"),
    )
    telemetry.install()
    async with ai.experimental_telemetry.span("rejected"):
        pass
    telemetry.flush()
    assert len(received) == 1  # no login redirect, no duplicated failed export
    assert "Failed to export span batch" in caplog.text
