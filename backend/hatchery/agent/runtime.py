"""Rotor client and Vercel worker for agent supervisors, threads, and serving.

The worker and the FastAPI app both install the deployment `Environment`: threads
need it for Git and sandboxes, and the app process needs it to start supervisors and
commit new agents. It is installed lazily so importing this module does no I/O and
needs no storage configuration. Installing it also makes every observed `main`
revision reconcile Git schedules.
"""

import os

import rotor
import rotor.platforms.vercel

from hatchery import environment, store
from hatchery.agent import supervisor
from hatchery.serve import scheduling
from hatchery.store import db

PROCESSES = [*supervisor.PROCESSES, *scheduling.PROCESSES]

database_url = os.environ.get("DATABASE_URL")
direct_url = os.environ.get("DATABASE_URL_UNPOOLED", database_url)
backends = rotor.Backends(
    db=(
        db._clean_dsn(database_url)
        if database_url
        else f"sqlite:///{store.data_dir() / 'rotor.db'}"
    ),
    live=db._clean_dsn(direct_url) if direct_url else None,
    record_message_activity=False,
)
platform = rotor.platforms.vercel.VercelRuntime(
    topic="hatchery-dispatcher-v1",
    maintenance_topic="hatchery-dispatcher-maintenance-v1",
    consumer_group="hatchery-dispatcher-v1",
    max_concurrency=8,
)
worker = rotor.Worker(backends, platform=platform)
for process in PROCESSES:
    worker.register(process)
client = rotor.Client(backends, wake=platform.dispatcher(backends))


def install() -> environment.Environment | None:
    """Install the deployment Environment once, if storage is configured."""
    try:
        return environment.Environment.current()
    except RuntimeError:
        pass
    if not os.environ.get("HATCHERY_STORAGE_REPO"):
        return None
    env = environment.Environment.from_env()
    env.workspaces.set_main_observer(scheduling.observe)
    env.install()
    return env


install()
