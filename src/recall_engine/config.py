"""Resolve CLI options into a single immutable Settings object."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


@dataclass(frozen=True)
class Settings:
    knowledge_paths: tuple[Path, ...]


def resolve_settings(
    knowledge_paths: Sequence[str] | None = None,
    fallback_paths: Sequence[Path] | None = None,
) -> Settings:
    """Resolve knowledge paths, falling back to an active wrap session's paths.

    Each path is expanded (`~`), must be an existing directory, and is resolved
    to an absolute path. Duplicates are dropped while the given order is kept.
    """
    raw_paths: list[str | Path] = list(knowledge_paths) if knowledge_paths else list(
        fallback_paths or []
    )
    if not raw_paths:
        raise ConfigError(
            "No knowledge paths configured; pass --knowledge-path for each "
            "knowledge directory."
        )

    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise ConfigError(
                f"--knowledge-path points to a missing directory: {path}"
            )
        absolute = path.resolve()
        if absolute not in seen:
            seen.add(absolute)
            resolved.append(absolute)

    return Settings(knowledge_paths=tuple(resolved))
