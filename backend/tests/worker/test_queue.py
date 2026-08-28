from worker import protocol, queue


async def test_send_uses_worker_topic_and_protocol_id(monkeypatch):
    seen = {}

    async def send(topic, payload, **options):
        seen.update(topic=topic, payload=payload, options=options)
        return "msg_1"

    monkeypatch.setattr(queue.vercel_queue, "send", send)
    command = protocol.command("wrk_1", 0, "task.launch", task_id="task_1")

    assert await queue.send(command) == "msg_1"
    assert seen["topic"] == protocol.command_topic("wrk_1")
    assert seen["payload"]["version"] == 1
    assert seen["options"]["idempotency_key"] == command.id
