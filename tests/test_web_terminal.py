import json
import os
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from recall_engine.web_terminal import create_app

API_KEY = "secret"


def make_client(tmp_path, script: str) -> TestClient:
    """Serve a fake agent shell script through the web terminal app."""
    agent = tmp_path / "agent.sh"
    agent.write_text(f"#!/bin/sh\n{script}\n")
    agent.chmod(0o755)
    return TestClient(create_app([str(agent)], dict(os.environ), API_KEY))


def read_until(ws, needle: bytes, limit: int = 200) -> bytes:
    """Collect binary PTY output until it contains needle."""
    received = b""
    for _ in range(limit):
        received += ws.receive_bytes()
        if needle in received:
            return received
    raise AssertionError(f"{needle!r} not in {received!r}")


def test_index_page_and_vendor_assets_are_served(tmp_path):
    client = make_client(tmp_path, "exit 0")
    page = client.get("/")
    assert page.status_code == 200
    assert "xterm" in page.text
    for asset in ("xterm.js", "xterm.css", "addon-fit.js", "addon-unicode11.js"):
        assert client.get(f"/vendor/xterm/{asset}").status_code == 200


@pytest.mark.parametrize("query", ("?api-key=wrong", "", f"?token={API_KEY}"))
def test_wrong_or_missing_api_key_is_rejected(tmp_path, query):
    client = make_client(tmp_path, "exit 0")
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(f"/ws{query}") as ws,
    ):
        ws.receive_bytes()
    assert exc.value.code == 1008
    assert exc.value.reason == "invalid api-key"


def test_initial_size_reaches_the_agent(tmp_path):
    client = make_client(tmp_path, "stty size; sleep 5")
    with client.websocket_connect(f"/ws?api-key={API_KEY}&cols=100&rows=30") as ws:
        read_until(ws, b"30 100")


def test_input_round_trip(tmp_path):
    client = make_client(tmp_path, "read line; echo got:$line; sleep 5")
    with client.websocket_connect(f"/ws?api-key={API_KEY}") as ws:
        ws.send_bytes(b"hi\r")
        read_until(ws, b"got:hi")


def test_resize_sends_sigwinch(tmp_path):
    # Needs a controlling tty: TIOCSWINSZ only signals the tty's foreground group.
    client = make_client(
        tmp_path,
        "trap 'stty size' WINCH\necho ready\nwhile :; do sleep 0.1; done",
    )
    with client.websocket_connect(f"/ws?api-key={API_KEY}&cols=80&rows=24") as ws:
        read_until(ws, b"ready")
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        read_until(ws, b"40 120")


def test_utf8_round_trip(tmp_path):
    client = make_client(tmp_path, "read line; echo got:$line; sleep 5")
    with client.websocket_connect(f"/ws?api-key={API_KEY}") as ws:
        ws.send_bytes("你好\r".encode())
        read_until(ws, "got:你好".encode())


def test_client_close_stops_the_agent(tmp_path):
    pid_file = tmp_path / "pid"
    client = make_client(
        tmp_path, f'echo $$ > "{pid_file}"\necho ready\nwhile :; do sleep 0.1; done'
    )
    with client.websocket_connect(f"/ws?api-key={API_KEY}") as ws:
        read_until(ws, b"ready")
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"agent {pid} still alive")


def test_agent_exit_closes_the_socket(tmp_path):
    client = make_client(tmp_path, "echo bye\nexit 3")
    with (
        client.websocket_connect(f"/ws?api-key={API_KEY}") as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        while True:
            ws.receive_bytes()
    assert exc.value.code == 1000
    assert "(3)" in exc.value.reason
