"""Serving: published agent HTTP routes and Git schedules, from `main` only.

Ported from agentmesh `serve/`. `host` splits `<agent>.<serve domain>` traffic off the
main app, `app` is the public route app, `service` runs handlers and jobs in the
agent's serve sandbox, `scheduling` owns the Rotor tickers, and `api` holds the
operator endpoints. Handler code talks back only through `hatchery.sdk`.
"""
