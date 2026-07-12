"""Validate and resolve the configured knowledge repo path."""

from pathlib import Path

from recall_engine.config import Settings


class RepoError(Exception):
    """Knowledge repo cannot be prepared."""


def ensure_repo(settings: Settings) -> Path:
    """Return the absolute path of an existing knowledge repo."""
    if not settings.repo_path.is_dir():
        raise RepoError(
            f"--local-knowledge-path points to a missing directory: {settings.repo_path}"
        )
    return settings.repo_path.resolve()
