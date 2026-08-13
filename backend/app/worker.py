"""Workflow worker entrypoint (see [[tool.vercel.workflows]] in pyproject.toml).

The builder imports this module and serves its queue subscriptions through a
generated vercel.queue.asgi_app() handler, with consumer groups introspected
from the SDK at build time (vercel/vercel#17236, needs CLI >= 58.9).
"""

from agent import telemetry

# Install before importing the workflow registry. Steps can then export spans,
# while replayed workflow code only sees the lightweight AI SDK span objects.
telemetry.install()

import agent  # noqa: E402
from agent import turn as _turn  # noqa: E402, F401  registers chat turn steps
from agent.tasks import parity as _parity  # noqa: E402, F401  registers parity steps

workflow = agent.workflow
