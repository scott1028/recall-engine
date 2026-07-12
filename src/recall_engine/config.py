"""Resolve CLI options into a single immutable Settings object."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


@dataclass(frozen=True)
class Settings:
    repo_path: Path
    drive_folder: str | None


def resolve_settings(
    local_knowledge_path: str | None = None,
    remote_knowledge_folder: str | None = None,
    fallback_repo_path: Path | None = None,
) -> Settings:
    """Resolve settings, falling back to an active wrap session's repo."""
    if local_knowledge_path:
        repo_path = Path(local_knowledge_path).expanduser()
    elif fallback_repo_path is not None:
        repo_path = fallback_repo_path
    else:
        raise ConfigError(
            "No knowledge repo configured; pass --local-knowledge-path."
        )

    return Settings(repo_path=repo_path, drive_folder=remote_knowledge_folder or None)
