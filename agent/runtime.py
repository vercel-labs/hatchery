"""ASGI entrypoint for the durable workflow worker."""

import os

import vercel.queue

from agent import telemetry

# Install before importing the workflow registry. Steps can then export spans,
# while replayed workflow code only sees the lightweight AI SDK span objects.
telemetry.install()

from agent import worker  # noqa: E402

# The deployed runtime bootstraps a worker service by looking for celery/
# dramatiq actors or vercel-workers subscriptions; the vercel.queue consumers
# that Workflows() registers are neither, so it needs an exported ASGI app.
app = vercel.queue.asgi_app(
    deployment=vercel.queue.ALL_DEPLOYMENTS,
    region=os.environ.get("VERCEL_REGION", "iad1"),
)

# The deployed builder currently names this trigger's consumer group after the
# pyproject entrypoint instead of using the SDK's "default" group. Mirror the
# workflow subscriptions under that generated group until the platform fix is
# live. SanitizedName prevents a second round of encoding.
_PLATFORM_GROUP = vercel.queue.SanitizedName(
    "__py__workflows_Sagent-runtime____app"
)
import vercel._internal.workflow.world as _wkf_world  # noqa: E402

if os.environ.get("VERCEL_DEPLOYMENT_ID"):
    for _callback in getattr(_wkf_world.get_world(), "_queue_callbacks", []):
        try:
            vercel.queue.subscribe(
                topic="__wkf_*", consumer_group=_PLATFORM_GROUP
            )(_callback)
        except vercel.queue.DuplicateSubscriptionError:
            pass
