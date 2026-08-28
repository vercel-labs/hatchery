"""Vercel Queue transport for worker commands."""

from vercel import queue as vercel_queue

from worker import protocol


async def send(command: protocol.Command) -> str | None:
    """Publish one durable worker command."""
    message_id = await vercel_queue.send(
        protocol.command_topic(command.worker_id),
        command.model_dump(mode="json"),
        idempotency_key=command.id,
    )
    return str(message_id) if message_id is not None else None
