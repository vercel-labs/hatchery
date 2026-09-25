"""Host-based ASGI dispatch between agent serve traffic and the Hatchery app.

Ported from agentmesh `serve/host.py`. Exactly `<agent>.<serve domain>` goes to the
public serve app, with the agent ID in `scope["state"]["hatchery_agent"]`. Every other
valid host keeps the normal app. Malformed or nested agent hosts fail closed and never
fall back to the app. Preview deployments may name the agent with `X-Hatchery-Agent`
instead (agentmesh's owner header); production never accepts it.
"""

import ipaddress
import re

import starlette.types

AGENT_HEADER = b"x-hatchery-agent"
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class _HostError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class HostDispatch:
    """Dispatch exactly one `<agent>.<domain>` label to the serve app."""

    def __init__(
        self,
        app: starlette.types.ASGIApp,
        serve_app: starlette.types.ASGIApp,
        *,
        domain: str,
        allow_agent_header: bool = False,
    ) -> None:
        self.app = app
        self.serve_app = serve_app
        normalized = domain.removesuffix(".").lower()
        if ":" in normalized or not _valid_dns_name(normalized):
            raise ValueError("domain must be a valid DNS hostname without a port")
        self.domain = normalized
        self.allow_agent_header = allow_agent_header

    async def __call__(
        self,
        scope: starlette.types.Scope,
        receive: starlette.types.Receive,
        send: starlette.types.Send,
    ) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            hosts = _header_values(scope, b"host")
            if len(hosts) != 1:
                raise _HostError(400, "exactly one Host header is required")
            agent = _host_agent(_hostname(hosts[0]), self.domain)
            if self.allow_agent_header:
                overrides = _header_values(scope, AGENT_HEADER)
                if len(overrides) > 1:
                    raise _HostError(400, "duplicate X-Hatchery-Agent header")
                if overrides:
                    agent = _agent_value(overrides[0])
        except _HostError as error:
            await _reject(scope, send, error.status_code, str(error))
            return
        if agent is None:
            await self.app(scope, receive, send)
            return
        routed = dict(scope)
        routed["state"] = {**scope.get("state", {}), "hatchery_agent": agent}
        await self.serve_app(routed, receive, send)


def _header_values(scope: starlette.types.Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


def _hostname(raw: bytes) -> str:
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise _HostError(400, "Host header must be ASCII") from error
    if not value or any(character.isspace() for character in value) or "," in value:
        raise _HostError(400, "malformed Host header")
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise _HostError(400, "malformed bracketed Host header")
        hostname = value[1:close]
        _validate_port(value[close + 1 :])
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError as error:
            raise _HostError(400, "malformed IPv6 Host header") from error
        return hostname.lower()
    if value.count(":") > 1:
        raise _HostError(400, "IPv6 Host headers must be bracketed")
    hostname, separator, port = value.partition(":")
    if separator:
        _validate_port(f":{port}")
    hostname = hostname.removesuffix(".").lower()
    if not hostname:
        raise _HostError(400, "malformed Host header")
    return hostname


def _validate_port(suffix: str) -> None:
    if not suffix:
        return
    port = suffix[1:]
    if not suffix.startswith(":") or not port.isdigit():
        raise _HostError(400, "malformed Host port")
    significant = port.lstrip("0") or "0"
    if len(significant) > 5 or (len(significant) == 5 and significant > "65535"):
        raise _HostError(400, "Host port is out of range")


def _host_agent(hostname: str, domain: str) -> str | None:
    suffix = f".{domain}"
    if hostname.endswith(suffix):
        agent = hostname[: -len(suffix)]
        if not _valid_dns_label(agent) or len(hostname) > 253:
            raise _HostError(421, "host does not name exactly one valid agent")
        return agent
    valid = hostname == "localhost" or _valid_dns_name(hostname)
    if not valid:
        try:
            ipaddress.ip_address(hostname)
            valid = True
        except ValueError:
            pass
    if not valid:
        raise _HostError(400, "malformed Host hostname")
    return None


def _valid_dns_name(hostname: str) -> bool:
    return len(hostname) <= 253 and all(_valid_dns_label(label) for label in hostname.split("."))


def _valid_dns_label(value: str) -> bool:
    return len(value) <= 63 and _DNS_LABEL.fullmatch(value) is not None


def _agent_value(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").lower()
    except UnicodeDecodeError as error:
        raise _HostError(400, "X-Hatchery-Agent must be ASCII") from error
    if not _valid_dns_label(value):
        raise _HostError(400, "X-Hatchery-Agent must be one valid DNS label")
    return value


async def _reject(
    scope: starlette.types.Scope, send: starlette.types.Send, status_code: int, detail: str
) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008, "reason": detail})
        return
    body = detail.encode()
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})
