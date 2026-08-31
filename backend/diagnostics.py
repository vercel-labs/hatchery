"""Small interactive diagnostics client for Hatchery terminals."""

import argparse
import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
import termios
import tty
import urllib.error
import urllib.request

import websockets.asyncio.client


def request(url: str, method: str = "GET") -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method)) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Hatchery returned {error.code}: {detail}") from error


async def attach(url: str, chat_id: str, collection: str, session_id: str) -> None:
    origin = url.rstrip("/")
    if origin.startswith("https://"):
        origin = "wss://" + origin.removeprefix("https://")
    elif origin.startswith("http://"):
        origin = "ws://" + origin.removeprefix("http://")
    columns, rows = os.get_terminal_size(sys.stdout.fileno())
    endpoint = (
        f"{origin}/api/chats/{chat_id}/{collection}/{session_id}/tty"
        f"?offset=0&cols={columns}&rows={rows}"
    )
    loop = asyncio.get_running_loop()
    resized = asyncio.Event()
    if hasattr(signal, "SIGWINCH"):
        loop.add_signal_handler(signal.SIGWINCH, resized.set)

    async with websockets.asyncio.client.connect(endpoint, max_size=None) as websocket:
        async def input_to_websocket() -> None:
            while data := await loop.run_in_executor(None, os.read, sys.stdin.fileno(), 65536):
                await websocket.send(json.dumps({
                    "type": "tty-input",
                    "body": {"data": base64.b64encode(data).decode()},
                }))

        async def websocket_to_output() -> None:
            async for raw in websocket:
                frame = json.loads(raw)
                if frame["type"] == "tty-output":
                    os.write(sys.stdout.fileno(), base64.b64decode(frame["body"]["data"]))
                elif frame["type"] == "exit":
                    return

        async def send_resizes() -> None:
            while True:
                await resized.wait()
                resized.clear()
                columns, rows = os.get_terminal_size(sys.stdout.fileno())
                await websocket.send(json.dumps({
                    "type": "resize",
                    "body": {"cols": columns, "rows": rows},
                }))

        previous = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        try:
            tasks = [
                asyncio.create_task(input_to_websocket()),
                asyncio.create_task(websocket_to_output()),
                asyncio.create_task(send_resizes()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, previous)
            if hasattr(signal, "SIGWINCH"):
                loop.remove_signal_handler(signal.SIGWINCH)


def main() -> None:
    parser = argparse.ArgumentParser(prog="hatchery", description="Hatchery diagnostics")
    commands = parser.add_subparsers(dest="resource", required=True)

    sandbox = commands.add_parser("sandbox")
    sandbox_commands = sandbox.add_subparsers(dest="action", required=True)
    shell = sandbox_commands.add_parser("shell", help="open a new shell in a sandbox")

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="action", required=True)
    attach_task = task_commands.add_parser("attach", help="attach to an existing subagent TTY")

    for command in (shell, attach_task):
        command.add_argument("--url", required=True, help="Hatchery deployment origin")
        command.add_argument("--chat", required=True)
    shell.add_argument("--sandbox", required=True)
    attach_task.add_argument("--task", required=True)
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("terminal attach requires an interactive TTY")

    try:
        if args.resource == "sandbox":
            terminal = request(
                f"{args.url.rstrip('/')}/api/chats/{args.chat}/sandboxes/{args.sandbox}/terminals",
                method="POST",
            )
            collection = "terminals"
            session_id = terminal["id"]
        else:
            collection = "subagents"
            session_id = args.task
        asyncio.run(attach(args.url, args.chat, collection, session_id))
    except (OSError, RuntimeError, websockets.exceptions.WebSocketException) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
