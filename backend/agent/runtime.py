"""Rotor client and Vercel worker for dispatcher processes."""

import os

import rotor
import rotor.platforms.vercel

import store
from agent import durable
from store import db


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
for process in durable.PROCESSES:
    worker.register(process)
client = rotor.Client(backends, wake=platform.dispatcher(backends))
