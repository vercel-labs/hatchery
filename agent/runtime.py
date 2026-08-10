"""Workflow worker entrypoint (see [[tool.vercel.workflows]] in pyproject.toml).

The builder imports this module and serves its queue subscriptions through a
generated vercel.queue.asgi_app() handler, with consumer groups introspected
from the SDK at build time (vercel/vercel#17236, needs CLI >= 58.9).
"""

from agent import telemetry

# Install before importing the workflow registry. Steps can then export spans,
# while replayed workflow code only sees the lightweight AI SDK span objects.
telemetry.install()

from agent import worker  # noqa: E402

workflow = worker.workflow
