from pathlib import Path

import pytest

from recall_engine.config import ConfigError, resolve_settings


def test_path_mode():
    settings = resolve_settings({"KNOWLEDGE_REPO_PATH": "~/workspace/knowledge"})
    assert settings.repo_mode == "path"
    assert settings.repo_path == Path.home() / "workspace" / "knowledge"
    assert settings.repo_ssh_url is None
    assert settings.ssh_key is None
    assert settings.drive_folder is None


def test_ssh_mode_defaults_to_cwd_clone_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_SSH": "git@github.com:developer/recall-engine.git"}
    )
    assert settings.repo_mode == "ssh"
    assert settings.repo_ssh_url == "git@github.com:developer/recall-engine.git"
    assert settings.repo_path == tmp_path / ".recall"


def test_both_repo_vars_set_raises():
    with pytest.raises(ConfigError, match="only one"):
        resolve_settings(
            {
                "KNOWLEDGE_REPO_PATH": "/tmp/knowledge",
                "KNOWLEDGE_REPO_SSH": "git@github.com:developer/recall-engine.git",
            }
        )


def test_neither_repo_var_set_raises():
    with pytest.raises(ConfigError, match="KNOWLEDGE_REPO_PATH or KNOWLEDGE_REPO_SSH"):
        resolve_settings({})


def test_ssh_key_expanduser():
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_PATH": "/tmp/knowledge", "SSH_KEY": "~/.ssh/id_rsa"}
    )
    assert settings.ssh_key == Path.home() / ".ssh" / "id_rsa"


def test_drive_folder_passthrough():
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_PATH": "/tmp/knowledge", "KNOWLEDGE_DRIVE_FOLDER": "folder-id-123"}
    )
    assert settings.drive_folder == "folder-id-123"


def test_fallback_repo_path_used_when_no_env():
    # A live wrap session's repo is reused when no repo env var is set.
    settings = resolve_settings({}, fallback_repo_path=Path("/tmp/active-repo"))
    assert settings.repo_mode == "path"
    assert settings.repo_path == Path("/tmp/active-repo")
    assert settings.repo_ssh_url is None


def test_fallback_repo_path_still_reads_drive_folder():
    settings = resolve_settings(
        {"KNOWLEDGE_DRIVE_FOLDER": "folder-id-123"},
        fallback_repo_path=Path("/tmp/active-repo"),
    )
    assert settings.repo_path == Path("/tmp/active-repo")
    assert settings.drive_folder == "folder-id-123"


def test_env_repo_path_wins_over_fallback():
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_PATH": "/tmp/env-repo"},
        fallback_repo_path=Path("/tmp/active-repo"),
    )
    assert settings.repo_path == Path("/tmp/env-repo")


def test_env_repo_ssh_wins_over_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_SSH": "git@github.com:developer/recall-engine.git"},
        fallback_repo_path=Path("/tmp/active-repo"),
    )
    assert settings.repo_mode == "ssh"


def test_neither_repo_var_nor_fallback_raises():
    with pytest.raises(ConfigError, match="KNOWLEDGE_REPO_PATH or KNOWLEDGE_REPO_SSH"):
        resolve_settings({}, fallback_repo_path=None)
