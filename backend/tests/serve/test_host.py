"""Host dispatch between agent serve traffic and the app. Ported from agentmesh
`tests/unit/test_serve_host.py`, plus the wiring into Hatchery's one FastAPI app."""

import httpx
import pytest
import starlette.types

from hatchery.app import server
from hatchery.serve import host

Calls = list[tuple[str, str, str | None]]


def recording_app(name: str, calls: Calls) -> starlette.types.ASGIApp:
    async def app(scope, receive, send) -> None:
        calls.append((name, scope["path"], scope.get("state", {}).get("hatchery_agent")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": name.encode()})

    return app


def dispatch(calls: Calls, **options) -> host.HostDispatch:
    return host.HostDispatch(
        recording_app("app", calls),
        recording_app("serve", calls),
        domain="agents.example.com",
        **options,
    )


async def request(
    app: starlette.types.ASGIApp, headers: list[tuple[bytes, bytes]], path: str = "/"
) -> tuple[int, bytes]:
    messages: list[starlette.types.Message] = []

    async def receive() -> starlette.types.Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: starlette.types.Message) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "server": ("ignored.invalid", 443),
            "client": ("127.0.0.1", 1234),
        },
        receive,
        send,
    )
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in messages)


async def test_one_agent_label_dispatches_every_path_and_sets_agent_state() -> None:
    calls: Calls = []

    status, body = await request(
        dispatch(calls), [(b"host", b"Alice.agents.example.com:8443")], "/private"
    )

    assert (status, body) == (200, b"serve")
    assert calls == [("serve", "/private", "alice")]


async def test_app_and_localhost_hosts_never_enter_serve() -> None:
    calls: Calls = []
    app = dispatch(calls)

    public = await request(app, [(b"host", b"console.example.net")], "/api/public")
    local = await request(app, [(b"host", b"localhost:3000")])
    ipv6 = await request(app, [(b"host", b"[::1]:8000")])

    assert public == (200, b"app")
    assert local == (200, b"app") and ipv6 == (200, b"app")
    assert calls == [("app", "/api/public", None), ("app", "/", None), ("app", "/", None)]


@pytest.mark.parametrize(
    "hostname",
    [b"team.alice.agents.example.com", b"-alice.agents.example.com", b".agents.example.com"],
)
async def test_malformed_or_nested_agent_suffix_never_falls_back_to_the_app(
    hostname: bytes,
) -> None:
    calls: Calls = []

    status, _ = await request(dispatch(calls), [(b"host", hostname)])

    assert status == 421
    assert calls == []


async def test_duplicate_or_malformed_host_is_rejected_before_dispatch() -> None:
    calls: Calls = []
    app = dispatch(calls)

    duplicate, _ = await request(app, [(b"host", b"localhost"), (b"Host", b"other.test")])
    malformed, _ = await request(app, [(b"host", b"[::1]:not-a-port")])

    assert duplicate == 400 and malformed == 400
    assert calls == []


async def test_agent_header_override_is_explicitly_non_production() -> None:
    production_calls: Calls = []
    preview_calls: Calls = []
    headers = [(b"host", b"localhost:3000"), (b"x-hatchery-agent", b"Dev-Agent")]

    assert await request(dispatch(production_calls), headers) == (200, b"app")
    assert await request(dispatch(preview_calls, allow_agent_header=True), headers) == (
        200,
        b"serve",
    )
    assert production_calls == [("app", "/", None)]
    assert preview_calls == [("serve", "/", "dev-agent")]


async def test_enabled_agent_override_rejects_duplicates_and_invalid_labels() -> None:
    calls: Calls = []
    app = dispatch(calls, allow_agent_header=True)

    duplicate, _ = await request(
        app,
        [
            (b"host", b"localhost"),
            (b"x-hatchery-agent", b"alice"),
            (b"X-Hatchery-Agent", b"bob"),
        ],
    )
    malformed, _ = await request(
        app, [(b"host", b"localhost"), (b"x-hatchery-agent", b"nested.agent")]
    )

    assert duplicate == 400 and malformed == 400
    assert calls == []


async def test_the_hatchery_app_routes_agent_hosts_to_serve_and_keeps_its_own_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def signed_out(_request):
        return None

    monkeypatch.setattr(server.auth, "current_user", signed_out)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as app:
        assert (await app.get("/api/agents")).status_code == 401
    async with httpx.AsyncClient(transport=transport, base_url="http://hatchery.localhost") as agent:
        # Public, no session: storage is not configured here, so serving fails closed.
        response = await agent.get("/api/agents")
    async with httpx.AsyncClient(transport=transport, base_url="http://a.b.localhost") as nested:
        assert (await nested.get("/api/health")).status_code == 421

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
