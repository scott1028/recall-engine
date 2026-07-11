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
    repo_path: Path
    drive_folder: str | None


def resolve_settings(
    env: Mapping[str, str] | None = None,
    fallback_repo_path: Path | None = None,
) -> Settings:
    """Resolve settings, falling back to an active wrap session's repo."""
    if env is None:
        env = os.environ

    repo_path_env = env.get("KNOWLEDGE_REPO_PATH")
    drive_folder = env.get("KNOWLEDGE_DRIVE_FOLDER") or None

    if repo_path_env:
        repo_path = Path(repo_path_env).expanduser()
    elif fallback_repo_path is not None:
        repo_path = fallback_repo_path
    else:
        raise ConfigError(
            "No knowledge repo configured; set KNOWLEDGE_REPO_PATH."
        )

    return Settings(repo_path=repo_path, drive_folder=drive_folder)
