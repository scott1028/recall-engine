"""Resolve env vars into a single immutable Settings object."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Invalid or incomplete environment configuration."""


@dataclass(frozen=True)
class Settings:
    repo_mode: str  # "path" | "ssh"
    repo_path: Path
    repo_ssh_url: str | None
    ssh_key: Path | None
    drive_folder: str | None


def resolve_settings(
    env: Mapping[str, str] | None = None,
    fallback_repo_path: Path | None = None,
) -> Settings:
    """Resolve settings from env; when no repo env var is set, fall back to
    fallback_repo_path (an already-running wrap session's repo) if given.
    """
    if env is None:
        env = os.environ

    repo_path_env = env.get("KNOWLEDGE_REPO_PATH")
    repo_ssh_env = env.get("KNOWLEDGE_REPO_SSH")

    if repo_path_env and repo_ssh_env:
        raise ConfigError(
            "Both KNOWLEDGE_REPO_PATH and KNOWLEDGE_REPO_SSH are set; set only one."
        )

    drive_folder = env.get("KNOWLEDGE_DRIVE_FOLDER") or None

    if not repo_path_env and not repo_ssh_env:
        if fallback_repo_path is not None:
            return Settings(
                repo_mode="path",
                repo_path=fallback_repo_path,
                repo_ssh_url=None,
                ssh_key=None,
                drive_folder=drive_folder,
            )
        raise ConfigError(
            "No knowledge repo configured; set KNOWLEDGE_REPO_PATH or KNOWLEDGE_REPO_SSH."
        )

    ssh_key_env = env.get("SSH_KEY")
    ssh_key = Path(ssh_key_env).expanduser() if ssh_key_env else None

    if repo_path_env:
        return Settings(
            repo_mode="path",
            repo_path=Path(repo_path_env).expanduser(),
            repo_ssh_url=None,
            ssh_key=ssh_key,
            drive_folder=drive_folder,
        )
    return Settings(
        repo_mode="ssh",
        repo_path=Path.cwd() / ".recall",
        repo_ssh_url=repo_ssh_env,
        ssh_key=ssh_key,
        drive_folder=drive_folder,
    )
