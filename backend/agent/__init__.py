"""agent: the durable workflows behind every chat.

One shared Workflows registry; agent.turn runs chat turns, agent.tasks.*
are the factory's workloads (parity today). The worker entrypoint
(app.worker) imports those modules to register their steps.
"""

import vercel.workflow

workflow = vercel.workflow.Workflows(
    sandbox_policy=vercel.workflow.SandboxPolicy(
        passthrough_modules=frozenset({"ai", "opentelemetry"}),
        cleanups=vercel.workflow.sandbox.ALL_CLEANUPS,
    )
)
