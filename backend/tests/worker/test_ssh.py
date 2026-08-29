import asyncio
import json
import os
import socket

import asyncssh
import pytest
import websockets

from worker import models, sandbox
from worker.daemon import main


async def connect(service):
    return await asyncssh.connect(
        "127.0.0.1",
        service.ssh_port,
        username="hatchery",
        password="",
        known_hosts=None,
    )


@pytest.fixture
async def ssh_service(tmp_path, monkeypatch):
    monkeypatch.setenv("HATCHERY_DAEMON_TOKEN", "secret")
    monkeypatch.setenv("HATCHERY_SSH_HOST_KEY", str(tmp_path / "ssh_host_key"))
    service = main.SSHService(str(tmp_path))
    await service.start(websocket_port=0, ssh_port=0)
    try:
        yield service
    finally:
        await service.stop()


async def test_ssh_exec_keeps_streams_exit_workspace_and_environment(ssh_service, tmp_path):
    (tmp_path / "marker").write_text("ok")
    async with await connect(ssh_service) as client:
        result = await client.run(
            "printf '%s:' \"$PROBE\"; cat marker; printf err >&2; exit 7",
            env={"PROBE": "present"},
            check=False,
        )
    assert result.stdout == "present:ok"
    assert result.stderr == "err"
    assert result.exit_status == 7


async def test_ssh_pty_receives_initial_geometry_and_resize(ssh_service):
    async with await connect(ssh_service) as client:
        process = await client.create_process(
            "stty size; read line; stty size",
            term_type="xterm-256color",
            term_size=(100, 30),
            encoding=None,
        )
        first = await process.stdout.readline()
        process.change_terminal_size(120, 40)
        process.stdin.write(b"go\n")
        output = await process.stdout.read()
        await process.wait()
    assert b"30 100" in first
    assert b"40 120" in output


async def test_ssh_forwarding_is_loopback_only(ssh_service):
    server = await asyncio.start_server(
        lambda reader, writer: asyncio.create_task(echo(reader, writer)),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        async with await connect(ssh_service) as client:
            reader, writer = await client.open_connection("127.0.0.1", port)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.close()
            with pytest.raises(asyncssh.ChannelOpenError):
                await client.open_connection("example.com", 80)
    finally:
        server.close()
        await server.wait_closed()


async def echo(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.close()


async def test_ssh_websocket_requires_auth_and_resumes_stream(ssh_service):
    url = f"ws://127.0.0.1:{ssh_service.websocket_port}"
    with pytest.raises(websockets.InvalidStatus) as denied:
        async with websockets.connect(url):
            pass
    assert denied.value.response.status_code == 401

    headers = {"authorization": "Bearer secret"}
    async with websockets.connect(url, additional_headers=headers) as first:
        handshake = json.loads(await first.recv())
        stream_id = handshake["stream_id"]
        await first.send((0).to_bytes(8, "big") + b"client bytes")
    stream = ssh_service.streams[stream_id]
    async with stream.condition:
        replay_offset = stream.base_offset + len(stream.output)
        stream.output.extend(b"server bytes")
        stream.condition.notify_all()
    async with websockets.connect(
        f"{url}?stream_id={stream_id}&offset={replay_offset}", additional_headers=headers
    ) as resumed:
        resumed_handshake = json.loads(await resumed.recv())
        replay = await resumed.recv()
    assert resumed_handshake["input_offset"] == len(b"client bytes")
    assert int.from_bytes(replay[:8], "big") == replay_offset
    assert replay[8:] == b"server bytes"


async def test_unknown_ssh_resume_is_not_found(ssh_service):
    url = f"ws://127.0.0.1:{ssh_service.websocket_port}?stream_id=missing"
    async with websockets.connect(
        url, additional_headers={"authorization": "Bearer secret"}
    ) as connection:
        with pytest.raises(websockets.ConnectionClosedError) as closed:
            await connection.recv()
    assert closed.value.code == 4404


def test_sandbox_ssh_returns_same_authenticated_route_for_every_environment():
    record = models.Worker(
        id="wrk",
        chat_id="chat",
        sandbox_name="hatchery-wrk",
        command_topic="hatchery-worker-wrk-commands-v1",
        title="box",
        status="running",
        spec=models.WorkerSpec(),
        routes=[models.Route(port=8788, url="https://ssh.example")],
        daemon_token="secret",
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    )
    assert sandbox.ssh(record) == (
        "wss://ssh.example",
        {"authorization": "Bearer secret"},
    )
