from dataclasses import fields
from pathlib import Path

import pytest

from recall_engine.config import ConfigError, Settings, resolve_settings


def test_path_mode():
    settings = resolve_settings(
        {
            "KNOWLEDGE_REPO_PATH": "~/workspace/knowledge",
            "KNOWLEDGE_DRIVE_FOLDER": "folder-id-123",
        }
    )

    assert settings.repo_path == Path.home() / "workspace" / "knowledge"
    assert settings.drive_folder == "folder-id-123"


def test_settings_contains_only_path_configuration():
    assert {field.name for field in fields(Settings)} == {
        "repo_path",
        "drive_folder",
    }


def test_missing_repo_config_raises():
    with pytest.raises(ConfigError, match="set KNOWLEDGE_REPO_PATH"):
        resolve_settings({})


def test_fallback_repo_path_and_env_precedence():
    fallback_repo = Path("/tmp/active-repo")
    settings = resolve_settings(
        {"KNOWLEDGE_DRIVE_FOLDER": "folder-id-123"},
        fallback_repo_path=fallback_repo,
    )

    assert settings.repo_path == fallback_repo
    assert settings.drive_folder == "folder-id-123"

    settings = resolve_settings(
        {"KNOWLEDGE_REPO_PATH": "/tmp/env-repo"},
        fallback_repo_path=fallback_repo,
    )

    assert settings.repo_path == Path("/tmp/env-repo")
