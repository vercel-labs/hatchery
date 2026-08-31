from worker import models, store


async def test_worker_records_round_trip():
    worker = models.Worker(
        id="wrk_1",
        chat_id="chat_1",
        sandbox_name="hatchery-wrk_1",
        command_topic="hatchery-worker-wrk_1-commands-v1",
        title="docs",
        status="running",
        spec=models.WorkerSpec(repos=["acme/docs"], ports=[3000]),
        routes=[models.Route(port=3000, url="https://docs.example")],
        daemon_token="secret",
        daemon_version=2,
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
    )

    await store.save(worker)

    assert await store.get(worker.id) == worker
    assert await store.list_all() == [worker]
    assert await store.delete(worker.id) is True
    assert await store.get(worker.id) is None
