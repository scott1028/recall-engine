import pytest

from recall_engine.config import Settings
from recall_engine.repo import RepoError, ensure_repo


def test_existing_repo_path_returns_resolved_directory(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    settings = Settings(repo_path=repo_path, drive_folder=None)

    assert ensure_repo(settings) == repo_path.resolve()


def test_missing_repo_path_raises(tmp_path):
    repo_path = tmp_path / "missing"
    settings = Settings(repo_path=repo_path, drive_folder=None)

    with pytest.raises(RepoError, match=str(repo_path)):
        ensure_repo(settings)
