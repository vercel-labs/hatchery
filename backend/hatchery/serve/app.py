"""The public ASGI app for agent routes, reached through `host.HostDispatch`.

Ported from agentmesh `serve/app.py`. Requests on `<agent>.<serve domain>/api/...` run
the agent's published handler. Cookies, the host, the agent override, and Vercel
credentials are never forwarded to handlers.
"""

import logging
import urllib.parse
import uuid

import fastapi
import fastapi.responses

from hatchery.serve import routing, service

_PRIVATE_HEADERS = {
    b"cookie",
    b"host",
    b"x-hatchery-agent",
    b"x-vercel-oidc-token",
    b"x-vercel-protection-bypass",
}
log = logging.getLogger(__name__)


def create_serve_app() -> fastapi.FastAPI:
    app = fastapi.FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def dispatch(request: fastapi.Request) -> fastapi.Response:
        from hatchery.agent import runtime

        agent_id = request.scope.get("state", {}).get("hatchery_agent")
        if not isinstance(agent_id, str):
            return fastapi.responses.JSONResponse(
                {"detail": "Agent host required"}, status_code=421
            )
        if runtime.install() is None:
            return fastapi.responses.JSONResponse({"detail": "Not found"}, status_code=404)
        serving = service.current()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > serving.env.config.serve.max_request_bytes:
                return fastapi.responses.JSONResponse(
                    {"detail": "Request body too large"}, status_code=413
                )
        headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in request.scope.get("headers", [])
            if name.lower() not in _PRIVATE_HEADERS
        ]
        try:
            result = await serving.invoke(
                agent_id,
                request_id=request.headers.get("x-request-id") or uuid.uuid4().hex,
                method=request.method,
                path=request.url.path,
                query=urllib.parse.parse_qsl(request.url.query, keep_blank_values=True),
                headers=headers,
                body=bytes(body),
            )
        except FileNotFoundError:
            return fastapi.responses.JSONResponse({"detail": "Not found"}, status_code=404)
        except Exception:
            log.exception("agent route invocation failed", extra={"agent_id": agent_id})
            return fastapi.responses.JSONResponse({"detail": "Handler failed"}, status_code=502)
        response = fastapi.Response(result.body, status_code=result.status)
        response.raw_headers.extend(
            (name.encode("ascii"), value.encode("latin-1")) for name, value in result.headers
        )
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    app.add_api_route("/api", dispatch, methods=list(routing.METHODS))
    app.add_api_route("/api/{path:path}", dispatch, methods=list(routing.METHODS))
    return app

