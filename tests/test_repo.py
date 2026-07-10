import subprocess
from pathlib import Path

import pytest

from recall_engine.config import Settings
from recall_engine.repo import RepoError, ensure_repo, resolve_ssh_key


def run_git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def path_settings(repo_path: Path) -> Settings:
    return Settings(
        repo_mode="path",
        repo_path=repo_path,
        repo_ssh_url=None,
        ssh_key=None,
        drive_folder=None,
    )


def ssh_settings(url: str, target: Path) -> Settings:
    return Settings(
        repo_mode="ssh",
        repo_path=target,
        repo_ssh_url=url,
        ssh_key=None,
        drive_folder=None,
    )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Fake HOME with a dummy ssh key so auto-detection never touches the real ~/.ssh."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("dummy key\n")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def remote_repo(tmp_path):
    """Local bare repo with one initial commit, standing in for GitHub."""
    bare = tmp_path / "remote.git"
    run_git("init", "--bare", str(bare))
    seed = tmp_path / "seed"
    run_git("clone", str(bare), str(seed))
    run_git("config", "user.email", "test@example.com", cwd=seed)
    run_git("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    run_git("add", "README.md", cwd=seed)
    run_git("commit", "-m", "init", cwd=seed)
    run_git("push", "origin", "HEAD", cwd=seed)
    return bare


# --- path mode ---


def test_path_mode_returns_resolved_dir(tmp_path):
    assert ensure_repo(path_settings(tmp_path)) == tmp_path.resolve()


def test_path_mode_missing_dir_raises(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(RepoError, match=str(missing)):
        ensure_repo(path_settings(missing))


# --- ssh mode against a local bare repo ---


def test_ssh_mode_clones_then_pulls(tmp_path, monkeypatch, fake_home, remote_repo):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".recall"
    settings = ssh_settings(str(remote_repo), target)

    repo = ensure_repo(settings)
    assert repo == target.resolve()
    assert (repo / "README.md").read_text() == "hello\n"

    # Push a new commit via a second clone, then ensure_repo must pull it.
    editor = tmp_path / "editor"
    run_git("clone", str(remote_repo), str(editor))
    run_git("config", "user.email", "test@example.com", cwd=editor)
    run_git("config", "user.name", "Test", cwd=editor)
    (editor / "new.md").write_text("new\n")
    run_git("add", "new.md", cwd=editor)
    run_git("commit", "-m", "add new.md", cwd=editor)
    run_git("push", "origin", "HEAD", cwd=editor)

    repo = ensure_repo(settings)
    assert (repo / "new.md").read_text() == "new\n"


def test_ssh_mode_origin_mismatch_raises(tmp_path, monkeypatch, fake_home, remote_repo):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".recall"
    ensure_repo(ssh_settings(str(remote_repo), target))
    with pytest.raises(RepoError, match="refusing to overwrite"):
        ensure_repo(ssh_settings("git@github.com:other/repo.git", target))


def test_ssh_mode_non_git_target_raises(tmp_path, monkeypatch, fake_home, remote_repo):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".recall"
    target.mkdir()
    with pytest.raises(RepoError, match="not a git repo"):
        ensure_repo(ssh_settings(str(remote_repo), target))


def test_ssh_mode_pull_failure_warns_and_keeps_checkout(
    tmp_path, monkeypatch, fake_home, remote_repo, capsys
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".recall"
    settings = ssh_settings(str(remote_repo), target)
    ensure_repo(settings)

    # Simulate an unreachable remote (offline) after the initial clone.
    remote_repo.rename(tmp_path / "remote-gone.git")

    repo = ensure_repo(settings)
    assert repo == target.resolve()
    assert (repo / "README.md").exists()
    assert "git pull failed" in capsys.readouterr().err


def test_ssh_mode_clone_failure_raises(tmp_path, monkeypatch, fake_home):
    monkeypatch.chdir(tmp_path)
    settings = ssh_settings(str(tmp_path / "no-remote.git"), tmp_path / ".recall")
    with pytest.raises(RepoError, match="git clone"):
        ensure_repo(settings)


# --- ssh key resolution ---


def test_key_detection_picks_id_rsa_when_only_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("dummy\n")
    monkeypatch.setenv("HOME", str(home))
    assert resolve_ssh_key(None) == home / ".ssh" / "id_rsa"


def test_key_detection_prefers_ed25519(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("dummy\n")
    (home / ".ssh" / "id_ed25519").write_text("dummy\n")
    monkeypatch.setenv("HOME", str(home))
    assert resolve_ssh_key(None) == home / ".ssh" / "id_ed25519"


def test_key_detection_none_found_lists_searched_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(RepoError) as excinfo:
        resolve_ssh_key(None)
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        assert str(home / ".ssh" / name) in str(excinfo.value)


def test_explicit_ssh_key_missing_file_raises(tmp_path):
    with pytest.raises(RepoError, match="SSH_KEY"):
        resolve_ssh_key(tmp_path / "no-such-key")


def test_explicit_ssh_key_wins(tmp_path):
    key = tmp_path / "my-key"
    key.write_text("dummy\n")
    assert resolve_ssh_key(key) == key


# --- git exclude injection ---


def test_exclude_injected_once_in_git_cwd(tmp_path, monkeypatch, fake_home, remote_repo):
    host = tmp_path / "host"
    host.mkdir()
    run_git("init", str(host))
    monkeypatch.chdir(host)
    settings = ssh_settings(str(remote_repo), host / ".recall")

    ensure_repo(settings)
    exclude = (host / ".git" / "info" / "exclude").read_text()
    assert ".recall/" in exclude.splitlines()

    ensure_repo(settings)
    exclude = (host / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".recall/") == 1


def test_non_git_cwd_skips_exclude(tmp_path, monkeypatch, fake_home, remote_repo):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.chdir(host)
    repo = ensure_repo(ssh_settings(str(remote_repo), host / ".recall"))
    assert repo.is_dir()
    assert not (host / ".git").exists()
