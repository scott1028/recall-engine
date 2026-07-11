from pathlib import Path
import pytest
from recall_engine.config import ConfigError, resolve_settings
def test_path_mode():
    settings = resolve_settings(
        {
            "KNOWLEDGE_REPO_PATH": "~/workspace/knowledge",
            "SSH_KEY": "~/.ssh/id_rsa",
            "KNOWLEDGE_DRIVE_FOLDER": "folder-id-123",
        }
    )
    assert settings.repo_mode == "path"
    assert settings.repo_path == Path.home() / "workspace" / "knowledge"
    assert settings.repo_ssh_url is None
    assert settings.ssh_key == Path.home() / ".ssh" / "id_rsa"
    assert settings.drive_folder == "folder-id-123"
def test_ssh_mode_defaults_to_cwd_clone_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_SSH": "git@github.com:developer/recall-engine.git"}
    )
    assert settings.repo_mode == "ssh"
    assert settings.repo_ssh_url == "git@github.com:developer/recall-engine.git"
    assert settings.repo_path == tmp_path / ".recall"
@pytest.mark.parametrize(
    ("env", "error_match"),
    (
        (
            {
                "KNOWLEDGE_REPO_PATH": "/tmp/knowledge",
                "KNOWLEDGE_REPO_SSH": "git@github.com:developer/recall-engine.git",
            },
            "only one",
        ),
        ({}, "KNOWLEDGE_REPO_PATH or KNOWLEDGE_REPO_SSH"),
    ),
)
def test_invalid_repo_config_raises(env, error_match):
    with pytest.raises(ConfigError, match=error_match):
        resolve_settings(env)
def test_fallback_repo_path_and_env_precedence(tmp_path, monkeypatch):
    fallback_repo = Path("/tmp/active-repo")
    settings = resolve_settings(
        {"KNOWLEDGE_DRIVE_FOLDER": "folder-id-123"},
        fallback_repo_path=fallback_repo,
    )
    assert settings.repo_mode == "path"
    assert settings.repo_path == fallback_repo
    assert settings.repo_ssh_url is None
    assert settings.drive_folder == "folder-id-123"
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_PATH": "/tmp/env-repo"},
        fallback_repo_path=fallback_repo,
    )
    assert settings.repo_path == Path("/tmp/env-repo")
    monkeypatch.chdir(tmp_path)
    settings = resolve_settings(
        {"KNOWLEDGE_REPO_SSH": "git@github.com:developer/recall-engine.git"},
        fallback_repo_path=fallback_repo,
    )
    assert settings.repo_mode == "ssh"
    assert settings.repo_path == tmp_path / ".recall"
