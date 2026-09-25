"""Static route discovery and matching. Ported from agentmesh `tests/unit/test_serve_routing.py`."""

from hatchery.serve import routing
from hatchery.workspace import files


def source(text: str) -> files.File:
    return files.File(text.encode())


def test_static_param_and_catch_all_routes_have_deterministic_precedence() -> None:
    table = routing.discover_routes(
        {
            "self/api/users/[...rest]/route.py": source("def GET(): pass"),
            "self/api/users/[user]/route.py": source("def GET(): pass"),
            "self/api/users/current/route.py": source("def GET(): pass"),
        }
    )

    current = table.resolve("/api/users/current", "GET")
    user = table.resolve("/api/users/ada", "GET")
    rest = table.resolve("/api/users/ada/files/avatar", "GET")

    assert current.status_code == 200 and current.route is not None
    assert current.route.path == "/api/users/current" and current.params == {}
    assert user.status_code == 200 and user.params == {"user": "ada"}
    assert rest.status_code == 200 and rest.params == {"rest": "ada/files/avatar"}


def test_discovery_reads_docstrings_and_top_level_sync_or_async_methods_without_execution() -> None:
    table = routing.discover_routes(
        {
            "self/api/reports/route.py": source(
                '"""Generate reports without loading this module."""\n'
                "raise RuntimeError('must not execute')\n"
                "async def POST(): pass\n"
                "def DELETE(): pass\n"
                "class Nested:\n    def GET(self): pass\n"
            )
        }
    )

    route = table.routes[0]
    assert route.methods == ("POST", "DELETE")
    assert route.description == "Generate reports without loading this module."
    assert route.handler_path == "self/api/reports/route.py"
    assert route.error is None


def test_same_shape_routes_are_unavailable_even_when_parameter_names_differ() -> None:
    table = routing.discover_routes(
        {
            "self/api/projects/[project]/route.py": source("def GET(): pass"),
            "self/api/projects/[slug]/route.py": source("def POST(): pass"),
        }
    )

    assert table.resolve("/api/projects/mesh", "GET").status_code == 404
    assert all(route.error and "ambiguous route shape" in route.error for route in table.routes)


def test_syntax_error_is_unavailable_metadata_without_hiding_other_routes() -> None:
    table = routing.discover_routes(
        {
            "self/api/broken/route.py": source("def GET(:\n    pass\n"),
            "self/api/healthy/route.py": source("async def PATCH(): pass\n"),
        }
    )

    broken, healthy = table.routes
    assert broken.path == "/api/broken" and broken.methods == ()
    assert broken.error is not None and broken.error.startswith("syntax error at line 1:")
    assert table.resolve("/api/broken", "GET").status_code == 404
    assert table.resolve("/api/healthy", "PATCH").route == healthy


def test_invalid_and_duplicate_parameters_are_unavailable_metadata() -> None:
    table = routing.discover_routes(
        {
            "self/api/[not-valid!]/route.py": source("def GET(): pass"),
            "self/api/[user]/notes/[user]/route.py": source("def GET(): pass"),
            "self/api/[...rest]/tail/route.py": source("def GET(): pass"),
        }
    )

    assert [route.error for route in table.routes] == [
        "catch-all segment must be last",
        "invalid route parameter name 'not-valid!'",
        "duplicate route parameter 'user'",
    ]
    assert table.resolve("/api/anything", "GET").status_code == 404


def test_invalid_static_segment_is_unavailable_metadata() -> None:
    table = routing.discover_routes({"self/api/not valid/route.py": source("def GET(): pass")})

    assert table.routes[0].error == "invalid static route segment 'not valid'"
    assert table.resolve("/api/not valid", "GET").status_code == 404


def test_resolution_distinguishes_missing_path_from_disallowed_method() -> None:
    table = routing.discover_routes(
        {"self/api/widgets/[widget]/route.py": source("def GET(): pass\nasync def PUT(): pass")}
    )

    missing = table.resolve("/api/gadgets/blue", "GET")
    disallowed = table.resolve("/api/widgets/blue%2Fgreen", "POST")

    assert missing.status_code == 404 and missing.route is None
    assert disallowed.status_code == 405
    assert disallowed.allowed_methods == ("GET", "PUT")
    assert disallowed.params == {"widget": "blue%2Fgreen"}
