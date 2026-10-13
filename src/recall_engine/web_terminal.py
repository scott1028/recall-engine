"""Serve the agent in a browser terminal (xterm.js over a WebSocket).

Each WebSocket connection spawns its own agent in a PTY. Binary frames carry
raw terminal bytes both ways; text frames carry JSON control messages (only
`{"type": "resize", "cols": N, "rows": N}` for now).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import signal
import struct
import subprocess
import termios
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

STATIC_DIR = Path(__file__).parent / "static"


def set_pty_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _set_controlling_tty() -> None:
    # Runs in the child after setsid(); stdin is the PTY slave. Without a
    # controlling tty, resize sends no SIGWINCH and /dev/tty cannot be opened.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def spawn_agent_pty(
    command: list[str], env: dict[str, str], cols: int, rows: int
) -> tuple[subprocess.Popen, int]:
    """Start command in a new session on a fresh PTY; return (process, master fd)."""
    master, slave = os.openpty()
    set_pty_size(master, cols, rows)
    try:
        proc = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env | {"TERM": "xterm-256color", "COLORTERM": "truecolor"},
            start_new_session=True,
            # Only a single ioctl runs in the child, so no lock can deadlock.
            preexec_fn=_set_controlling_tty,  # noqa: PLW1509
        )
    except Exception:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


async def stop_agent(proc: subprocess.Popen) -> int:
    """Hang up the agent's process group; SIGKILL it if it lingers. Return its code."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            await asyncio.to_thread(proc.wait, 3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            await asyncio.to_thread(proc.wait)
    return proc.returncode


def create_app(command: list[str], env: dict[str, str], api_key: str) -> Starlette:
    """Build the web terminal app; every /ws connection runs its own agent."""

    async def terminal_socket(websocket: WebSocket) -> None:
        if not secrets.compare_digest(
            websocket.query_params.get("api-key", "").encode(), api_key.encode()
        ):
            # Accept first: a pre-accept close becomes an HTTP 403 that the
            # browser only sees as code 1006.
            await websocket.accept()
            await websocket.close(1008, reason="invalid api-key")
            return
        await websocket.accept()
        cols = int(websocket.query_params.get("cols", 80))
        rows = int(websocket.query_params.get("rows", 24))
        proc, master = spawn_agent_pty(command, env, cols, rows)
        loop = asyncio.get_running_loop()
        output: asyncio.Queue[bytes | None] = asyncio.Queue()

        def on_readable() -> None:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO once every slave fd is closed
                chunk = b""
            if not chunk:
                loop.remove_reader(master)
                output.put_nowait(None)
                return
            output.put_nowait(chunk)

        async def pump_output() -> None:
            while (chunk := await output.get()) is not None:
                await websocket.send_bytes(chunk)

        async def pump_input() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    os.write(master, message["bytes"])
                elif message.get("text"):
                    control = json.loads(message["text"])
                    if control.get("type") == "resize":
                        set_pty_size(master, int(control["cols"]), int(control["rows"]))

        loop.add_reader(master, on_readable)
        sender = asyncio.create_task(pump_output())
        receiver = asyncio.create_task(pump_input())
        try:
            done, _ = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            if sender in done:
                # Agent closed the PTY: report its exit code to the browser.
                receiver.cancel()
                try:
                    await asyncio.to_thread(proc.wait, 3)
                except subprocess.TimeoutExpired:
                    pass
                code = await stop_agent(proc)
                await websocket.close(1000, reason=f"agent exited ({code})")
        finally:
            sender.cancel()
            receiver.cancel()
            loop.remove_reader(master)
            os.close(master)
            await stop_agent(proc)

    return Starlette(
        routes=[
            WebSocketRoute("/ws", terminal_socket),
            Mount("/", StaticFiles(directory=STATIC_DIR, html=True)),
        ]
    )


def run_web(
    command: list[str], env: dict[str, str], host: str, port: int, api_key: str
) -> int:
    """Serve the web terminal until Ctrl+C (or SIGHUP/SIGTERM); return 0."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(command, env, api_key),
            host=host,
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=5,
        )
    )
    # uvicorn ignores SIGHUP, and after shutdown it re-raises a captured SIGTERM
    # under the previous handler (SIG_DFL would kill us). Pre-install handle_exit
    # for both so the caller can still tear down the injected config.
    previous = {
        sig: signal.signal(sig, server.handle_exit)
        for sig in (signal.SIGHUP, signal.SIGTERM)
    }
    try:
        server.run()
    except KeyboardInterrupt:  # uvicorn re-raises the captured SIGINT
        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
