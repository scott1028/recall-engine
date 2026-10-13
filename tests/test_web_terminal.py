import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from recall_engine.web_terminal import build_sandbox_prefix, create_app

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


def bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


def test_sandbox_prefix_mount_order(monkeypatch, tmp_path):
    # Later mounts cover earlier ones: overlay $HOME, then re-lock the project
    # and knowledge paths, then open only <project>/.tmp.
    home = tmp_path / "home"
    project = home / "project"
    knowledge = [home / "notes-a", tmp_path / "notes-b"]
    for path in (project, *knowledge):
        path.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    prefix = build_sandbox_prefix(project, knowledge)
    assert (project / ".tmp").is_dir()
    assert Path(prefix[0]).name == "bwrap"
    assert prefix[-1] == "--"
    args = " ".join(prefix)
    order = [
        "--ro-bind / /",
        "--dev-bind /dev /dev",
        "--tmpfs /tmp",
        f"--overlay-src {home} --tmp-overlay {home}",
        f"--ro-bind {project} {project}",
        f"--ro-bind {knowledge[0]} {knowledge[0]}",
        f"--ro-bind {knowledge[1]} {knowledge[1]}",
        f"--bind {project / '.tmp'} {project / '.tmp'}",
    ]
    positions = [args.index(part) for part in order]
    assert positions == sorted(positions)
    assert "--die-with-parent" in prefix
    assert f"--chdir {project}" in args
    # Both would break the PTY session (ctty/SIGWINCH, killpg).
    assert "--new-session" not in prefix
    assert "--unshare-pid" not in prefix


@pytest.mark.skipif(not bwrap_usable(), reason="bubblewrap not usable")
def test_sandbox_locks_everything_but_dot_tmp(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    knowledge = tmp_path / "notes"
    for path in (home, project, knowledge):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    host_tmp = Path("/tmp") / f"recall-sandbox-{uuid.uuid4().hex}"
    # The script lives in the (read-only, still readable) project dir: the
    # sandbox hides the host /tmp that holds tmp_path's other files.
    agent = project / "agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        f'touch "{project}/x" 2>/dev/null && echo project:written || echo project:ro\n'
        f'touch "{knowledge}/x" 2>/dev/null && echo notes:written || echo notes:ro\n'
        'touch "$HOME/x" && echo home:ok\n'
        f'touch "{host_tmp}" && echo tmp:ok\n'
        f'touch "{project}/.tmp/x" && echo dottmp:ok\n'
        "trap 'stty size' WINCH\n"
        "stty size\n"
        "echo ready\n"
        "while :; do sleep 0.1; done\n"
    )
    agent.chmod(0o755)
    command = [*build_sandbox_prefix(project, [knowledge]), str(agent)]
    client = TestClient(create_app(command, dict(os.environ), API_KEY))
    with client.websocket_connect(f"/ws?api-key={API_KEY}&cols=100&rows=30") as ws:
        output = read_until(ws, b"ready")
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        read_until(ws, b"40 120")
    for line in (b"project:ro", b"notes:ro", b"home:ok", b"tmp:ok", b"dottmp:ok", b"30 100"):
        assert line in output
    assert not (project / "x").exists()
    assert not (knowledge / "x").exists()
    assert not (home / "x").exists()
    assert not host_tmp.exists()
    assert (project / ".tmp" / "x").exists()


@pytest.mark.parametrize("under_run", (True, False))
def test_sandbox_prefix_hides_run_but_keeps_dns(monkeypatch, tmp_path, under_run):
    # A read-only mount does not stop connect() on host sockets under /run
    # (systemd --user, D-Bus, docker), so /run is replaced by a tmpfs; only the
    # dir holding the real resolv.conf comes back, read-only.
    monkeypatch.setenv("HOME", str(tmp_path))
    resolv_dir = Path("/run/fake-resolve") if under_run else tmp_path / "etc"
    resolv = tmp_path / "resolv.conf"
    resolv.symlink_to(resolv_dir / "stub-resolv.conf")
    monkeypatch.setattr("recall_engine.web_terminal.RESOLV_CONF", resolv)
    project = tmp_path / "project"
    project.mkdir()
    args = " ".join(build_sandbox_prefix(project, []))
    assert args.index("--ro-bind / /") < args.index("--tmpfs /run")
    rebind = f"--ro-bind-try {resolv_dir} {resolv_dir}"
    if under_run:
        assert args.index("--tmpfs /run") < args.index(rebind)
    else:
        assert rebind not in args
    for name in (
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "SSH_AUTH_SOCK",
    ):
        assert f"--unsetenv {name}" in args


@pytest.mark.skipif(not bwrap_usable(), reason="bubblewrap not usable")
def test_sandbox_hides_host_sockets(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    for path in (home, project):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env = dict(os.environ) | {
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }
    script = (
        'test -e "/run/user/$(id -u)" && echo user-run:visible\n'
        "test -e /run/docker.sock && echo docker:visible\n"
        "cat /etc/resolv.conf >/dev/null 2>&1 && echo resolv:readable\n"
        'echo "display=${DISPLAY-unset} dbus=${DBUS_SESSION_BUS_ADDRESS-unset}"\n'
    )
    probe = subprocess.run(
        [*build_sandbox_prefix(project, []), "sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "user-run:visible" not in probe.stdout
    assert "docker:visible" not in probe.stdout
    if os.access("/etc/resolv.conf", os.R_OK):
        assert "resolv:readable" in probe.stdout
    assert "display=unset dbus=unset" in probe.stdout
