"""Static discovery and matching for agent API routes.

Ported from agentmesh `serve/routing.py`. `self/api/**/route.py` is parsed with `ast`
and never imported. Top-level `GET`/`POST`/`PUT`/`PATCH`/`DELETE` functions declare
methods. Static segments beat `[param]`, which beats a final `[...rest]`. Invalid or
ambiguous routes stay listed for the operator but never match.
"""

import ast
import dataclasses
import re
import typing

from hatchery.workspace import files

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_STATIC_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*")
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

type SegmentKind = typing.Literal["static", "param", "catch_all"]
type Segment = tuple[SegmentKind, str]


@dataclasses.dataclass(frozen=True)
class Route:
    """Metadata obtained without importing or executing a route module."""

    handler_path: str
    path: str
    methods: tuple[str, ...]
    description: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None


@dataclasses.dataclass(frozen=True)
class RouteResolution:
    """The HTTP outcome of resolving a path and method."""

    status_code: typing.Literal[200, 404, 405]
    route: Route | None = None
    params: dict[str, str] = dataclasses.field(default_factory=dict)
    allowed_methods: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class _Candidate:
    route: Route
    segments: tuple[Segment, ...] | None


class RouteTable:
    """A deterministic collection of discovered routes."""

    def __init__(self, candidates: tuple[_Candidate, ...]) -> None:
        self.routes = tuple(candidate.route for candidate in candidates)
        rank = {"static": 0, "param": 1, "catch_all": 2}
        self._available = tuple(
            sorted(
                (c for c in candidates if c.route.available and c.segments is not None),
                key=lambda c: tuple(
                    (rank[kind], value if kind == "static" else "")
                    for kind, value in c.segments or ()
                ),
            )
        )

    def resolve(self, path: str, method: str) -> RouteResolution:
        """Match an ASGI path rooted at `/api`; tell a missing path from a wrong method."""
        if path == "/api":
            parts: tuple[str, ...] = ()
        elif path.startswith("/api/"):
            parts = tuple(path.removeprefix("/api/").split("/"))
            if not all(parts):
                return RouteResolution(404)
        else:
            return RouteResolution(404)
        for candidate in self._available:
            params = _match(candidate.segments or (), parts)
            if params is None:
                continue
            route = candidate.route
            if method.upper() not in route.methods:
                return RouteResolution(405, route, params, route.methods)
            return RouteResolution(200, route, params, route.methods)
        return RouteResolution(404)


def discover_routes(tree: files.Tree) -> RouteTable:
    """Discover `self/api/**/route.py` files using syntax inspection only."""
    candidates: list[_Candidate] = []
    paths = sorted(
        path
        for path in tree
        if path == "self/api/route.py"
        or (path.startswith("self/api/") and path.endswith("/route.py"))
    )
    for handler_path in paths:
        relative = handler_path.removeprefix("self/").removesuffix("/route.py")
        segments, segment_error = _parse_segments(relative.split("/")[1:])
        methods: tuple[str, ...] = ()
        description: str | None = None
        source_error: str | None = None
        try:
            if len(tree[handler_path].content) > 256 * 1024:
                raise ValueError("route source exceeds 256 KiB")
            source = tree[handler_path].content.decode("utf-8")
            module = ast.parse(source, filename=handler_path)
            description = ast.get_docstring(module, clean=True)
            defined = {
                node.name
                for node in module.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            methods = tuple(method for method in METHODS if method in defined)
            if not methods:
                source_error = "route defines no supported methods"
        except UnicodeDecodeError:
            source_error = "route source is not UTF-8"
        except SyntaxError as error:
            location = f" at line {error.lineno}" if error.lineno is not None else ""
            source_error = f"syntax error{location}: {error.msg}"
        except ValueError as error:
            source_error = str(error)
        route = Route(
            handler_path=handler_path,
            path=f"/{relative}",
            methods=methods,
            description=description,
            error=segment_error or source_error,
        )
        candidates.append(_Candidate(route, segments))

    by_shape: dict[tuple[tuple[str, str], ...], list[int]] = {}
    for index, candidate in enumerate(candidates):
        if candidate.segments is not None:
            shape = tuple(
                (kind, value if kind == "static" else "") for kind, value in candidate.segments
            )
            by_shape.setdefault(shape, []).append(index)
    for indexes in by_shape.values():
        if len(indexes) < 2:
            continue
        ambiguous = ", ".join(candidates[index].route.path for index in indexes)
        for index in indexes:
            candidate = candidates[index]
            error = candidate.route.error or f"ambiguous route shape: {ambiguous}"
            candidates[index] = dataclasses.replace(
                candidate, route=dataclasses.replace(candidate.route, error=error)
            )
    return RouteTable(tuple(candidates))


def _parse_segments(parts: list[str]) -> tuple[tuple[Segment, ...] | None, str | None]:
    segments: list[Segment] = []
    params: set[str] = set()
    for index, part in enumerate(parts):
        if part.startswith("[...") and part.endswith("]"):
            name = part[4:-1]
            if index != len(parts) - 1:
                return None, "catch-all segment must be last"
            kind: SegmentKind = "catch_all"
        elif part.startswith("[") and part.endswith("]"):
            name = part[1:-1]
            kind = "param"
        else:
            if not _STATIC_SEGMENT.fullmatch(part):
                return None, f"invalid static route segment {part!r}"
            segments.append(("static", part))
            continue
        if not _PARAM_NAME.fullmatch(name):
            return None, f"invalid route parameter name {name!r}"
        if name in params:
            return None, f"duplicate route parameter {name!r}"
        params.add(name)
        segments.append((kind, name))
    return tuple(segments), None


def _match(segments: tuple[Segment, ...], parts: tuple[str, ...]) -> dict[str, str] | None:
    params: dict[str, str] = {}
    position = 0
    for kind, value in segments:
        if kind == "catch_all":
            if position >= len(parts):
                return None
            params[value] = "/".join(parts[position:])
            position = len(parts)
            break
        if position >= len(parts):
            return None
        if kind == "static":
            if parts[position] != value:
                return None
        else:
            params[value] = parts[position]
        position += 1
    return params if position == len(parts) else None
