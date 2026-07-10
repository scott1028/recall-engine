"""Environment diagnostics: one line per check, with actionable fixes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from recall_engine.agents import AGENTS
from recall_engine.config import ConfigError, resolve_settings
from recall_engine.drive import DriveError, build_drive_service, execute
from recall_engine.repo import RepoError, resolve_ssh_key


def _ok(name: str, detail: str) -> None:
    print(f"[ok] {name}: {detail}")


def _fail(name: str, detail: str, fix: str) -> None:
    print(f"[fail] {name}: {detail}")
    for line in fix.splitlines():
        print(f"    {line}")


def _check_git() -> bool:
    path = shutil.which("git")
    if path is None:
        _fail("git", "not found on PATH", "install git (apt/brew install git)")
        return False
    _ok("git", path)
    return True


def _check_agents() -> bool:
    """Report each supported agent CLI; pass if at least one is installed."""
    found = False
    for name in AGENTS:
        path = shutil.which(name)
        if path is None:
            print(f"[skip] {name}: not found on PATH")
        else:
            _ok(name, path)
            found = True
    if not found:
        _fail(
            "agent CLIs",
            f"none of {'/'.join(AGENTS)} found on PATH",
            "\n".join(spec.install_hint for spec in AGENTS.values()),
        )
    return found


def _check_ssh_key() -> bool:
    ssh_key_env = os.environ.get("SSH_KEY")
    ssh_key = Path(ssh_key_env).expanduser() if ssh_key_env else None
    try:
        key = resolve_ssh_key(ssh_key)
    except RepoError as exc:
        _fail("ssh key", "no usable key", str(exc))
        return False
    _ok("ssh key", str(key))
    return True


def _check_repo_config() -> bool:
    try:
        settings = resolve_settings()
    except ConfigError as exc:
        _fail("repo config", "not configured", str(exc))
        return False
    if settings.repo_mode == "path":
        _ok("repo config", f"path mode: {settings.repo_path}")
    else:
        _ok("repo config", f"ssh mode: {settings.repo_ssh_url} -> {settings.repo_path}")
    return True


def _check_drive_access() -> bool:
    try:
        service = build_drive_service()
        # Trivial call to prove the Drive scope actually works; no folder needed.
        execute(service.files().list(pageSize=1, fields="files(id)"))
    except DriveError as exc:
        _fail("gcloud auth", "Drive access failed", str(exc))
        return False
    except Exception as exc:  # e.g. network down; never show a stack trace
        _fail(
            "gcloud auth",
            f"Drive API call failed ({type(exc).__name__}: {exc})",
            "check network connectivity and retry",
        )
        return False
    _ok("gcloud auth", "credentials with Drive scope verified")
    return True


def _report_drive_folder() -> None:
    """Informational only: the folder is needed just for sync."""
    folder = os.environ.get("KNOWLEDGE_DRIVE_FOLDER")
    if folder:
        _ok("drive folder", folder)
    else:
        print("[skip] drive folder: KNOWLEDGE_DRIVE_FOLDER not set (needed only for sync)")


def run_doctor() -> bool:
    """Run every check; return True only when all required checks pass."""
    results = [
        _check_git(),
        _check_agents(),
        _check_ssh_key(),
        _check_repo_config(),
        _check_drive_access(),
    ]
    _report_drive_folder()
    return all(results)
