"""Global, machine-wide MCP server lifecycle.

One recall-engine MCP server serves every wrap session on the host. A PID file
in the temp dir (namespaced per uid) records the running server plus the wrap
processes that depend on it (owners, a refcount). The first `wrap` spawns the
server; later wraps reuse it; the last owner to leave shuts it down. A single
flock serializes read-modify-write of the PID file across processes.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from recall_engine.state import atomic_write_json, file_lock, is_pid_alive

HOST = "127.0.0.1"


class SupervisorError(Exception):
    """The shared MCP server could not be started or reached."""


@dataclass(frozen=True)
class ServerInfo:
    url: str
    port: int
    token: str
    pid: int
    repo_first: bool = False


@dataclass(frozen=True)
class ServerStatus:
    url: str
    pid: int
    owners: list[int]
    reachable: bool


def _state_path() -> Path:
    return Path(tempfile.gettempdir()) / f"recall-engine-mcp-{os.getuid()}.json"


def _lock_path() -> Path:
    return Path(tempfile.gettempdir()) / f"recall-engine-mcp-{os.getuid()}.lock"


def _log_path() -> Path:
    return Path(tempfile.gettempdir()) / f"recall-engine-mcp-{os.getuid()}.log"


def log_path() -> Path:
    """Public accessor for the shared server log; the background sync writes here."""
    return _log_path()


def _read_state() -> dict | None:
    path = _state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _url(port: int) -> str:
    return f"http://{HOST}:{port}/mcp"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _is_listening(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_healthy(port: int, pid: int, timeout: float = 15.0) -> bool:
    """Poll until the server accepts TCP connections; fail fast if it dies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return False
        if _is_listening(port):
            return True
        time.sleep(0.1)
    return False


def _spawn_server(port: int, token: str) -> int:
    """Launch the MCP server detached so it outlives this wrap process."""
    log = open(_log_path(), "ab")  # noqa: SIM115 (kept open for the child)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "recall_engine",
            "mcp-serve",
            "--host",
            HOST,
            "--port",
            str(port),
            "--token",
            token,
        ],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _server_alive(record: dict) -> bool:
    pid = record.get("pid")
    port = record.get("port")
    return (
        isinstance(pid, int)
        and isinstance(port, int)
        and is_pid_alive(pid)
        and _is_listening(port)
    )


def server_status() -> ServerStatus | None:
    """Snapshot the recorded server for diagnostics; None when no state file.

    Deliberately lock-free: a read-only observation must never block a wrap
    session that holds the lock.
    """
    record = _read_state()
    if record is None:
        return None
    return ServerStatus(
        url=record.get("url", ""),
        pid=record.get("pid", 0),
        owners=list(record.get("owners", [])),
        reachable=_server_alive(record),
    )


def _register_repo_owner(record: dict, repo: Path | None) -> bool:
    """Add this pid to `repo`'s owner list; return True if it's the first live one.

    Dead owner pids are pruned first, so a repo whose sessions all exited counts
    as first again on the next wrap.
    """
    if repo is None:
        return False
    repo_owners = record.setdefault("repo_owners", {})
    key = str(repo)
    live = [p for p in repo_owners.get(key, []) if is_pid_alive(p)]
    repo_first = not live
    if os.getpid() not in live:
        live.append(os.getpid())
    repo_owners[key] = live
    return repo_first


def ensure_server(repo: Path | None = None, token: str | None = None) -> ServerInfo:
    """Reuse the running server or spawn one; register this process as an owner.

    When `repo` is given, per-repo owners are tracked too, so the first wrap to
    bring a repo online (no live owner yet) is reported via `ServerInfo.repo_first`.
    """
    with file_lock(_lock_path()):
        record = _read_state()
        if record and _server_alive(record):
            owners = sorted(set(record.get("owners", [])) | {os.getpid()})
            record["owners"] = owners
            repo_first = _register_repo_owner(record, repo)
            atomic_write_json(_state_path(), record)
            return ServerInfo(
                url=record["url"],
                port=record["port"],
                token=record["token"],
                pid=record["pid"],
                repo_first=repo_first,
            )

        # No live/healthy server: spawn a fresh one.
        token = token or secrets.token_hex(16)
        port = _free_port()
        pid = _spawn_server(port, token)
        if not _wait_healthy(port, pid):
            raise SupervisorError(
                f"MCP server failed to start on {HOST}:{port}; see {_log_path()}"
            )
        record = {
            "pid": pid,
            "port": port,
            "url": _url(port),
            "token": token,
            "owners": [os.getpid()],
            "repo_owners": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        repo_first = _register_repo_owner(record, repo)
        atomic_write_json(_state_path(), record)
        return ServerInfo(
            url=record["url"], port=port, token=token, pid=pid, repo_first=repo_first
        )


def release_server(owner_pid: int | None = None, *, force: bool = False) -> bool:
    """Drop this owner; the last live owner stops the server. Idempotent.

    Returns True if a state file existed, False otherwise.
    """
    with file_lock(_lock_path()):
        record = _read_state()
        if record is None:
            return False

        # Drop this owner from per-repo tracking (pruning dead pids too), so a
        # repo whose sessions all exit re-triggers the first-wrap sync next time.
        repo_owners = record.get("repo_owners", {})
        for key in list(repo_owners):
            live = [p for p in repo_owners[key] if is_pid_alive(p) and p != owner_pid]
            if live:
                repo_owners[key] = live
            else:
                del repo_owners[key]
        record["repo_owners"] = repo_owners

        if not force:
            remaining = [
                p
                for p in record.get("owners", [])
                if is_pid_alive(p) and p != owner_pid
            ]
            if remaining:
                record["owners"] = remaining
                atomic_write_json(_state_path(), record)
                return True

        # Last owner (or force): stop the server and remove the state file.
        pid = record.get("pid")
        if isinstance(pid, int) and is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        _state_path().unlink(missing_ok=True)
        return True
