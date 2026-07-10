"""Resolve the knowledge repo: local path mode or SSH clone+pull mode."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from recall_engine.config import Settings

SSH_KEY_CANDIDATES = ("id_ed25519", "id_ecdsa", "id_rsa")
EXCLUDE_ENTRY = ".recall/"


class RepoError(Exception):
    """Knowledge repo cannot be prepared."""


def resolve_ssh_key(ssh_key: Path | None) -> Path:
    """Return the SSH key to use: explicit SSH_KEY wins, else auto-detect in ~/.ssh."""
    if ssh_key is not None:
        if not ssh_key.is_file():
            raise RepoError(f"SSH_KEY points to a missing file: {ssh_key}")
        return ssh_key
    searched = [Path.home() / ".ssh" / name for name in SSH_KEY_CANDIDATES]
    for candidate in searched:
        if candidate.is_file():
            return candidate
    raise RepoError(
        "No SSH key found; searched: "
        + ", ".join(str(path) for path in searched)
        + ". Set SSH_KEY to use a specific key."
    )


def ensure_repo(settings: Settings) -> Path:
    """Return the absolute path of a ready-to-use knowledge repo."""
    if settings.repo_mode == "path":
        if not settings.repo_path.is_dir():
            raise RepoError(
                f"KNOWLEDGE_REPO_PATH points to a missing directory: {settings.repo_path}"
            )
        return settings.repo_path.resolve()

    repo = settings.repo_path
    url = settings.repo_ssh_url
    key = resolve_ssh_key(settings.ssh_key)
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes",
    }

    if not repo.exists():
        clone = subprocess.run(
            ["git", "clone", url, str(repo)],
            env=env,
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise RepoError(f"git clone of {url} failed:\n{clone.stderr.strip()}")
    else:
        if not (repo / ".git").exists():
            raise RepoError(
                f"{repo} exists but is not a git repo; move it aside or use KNOWLEDGE_REPO_PATH."
            )
        origin = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            env=env,
            capture_output=True,
            text=True,
        )
        if origin.returncode != 0:
            raise RepoError(
                f"Cannot read origin of {repo}:\n{origin.stderr.strip()}"
            )
        if origin.stdout.strip() != url:
            raise RepoError(
                f"{repo} has origin '{origin.stdout.strip()}' but KNOWLEDGE_REPO_SSH is "
                f"'{url}'; refusing to overwrite. Remove the directory to re-clone."
            )
        pull = subprocess.run(
            ["git", "-C", str(repo), "pull", "--ff-only"],
            env=env,
            capture_output=True,
            text=True,
        )
        if pull.returncode != 0:
            print(
                f"warning: git pull failed, using existing checkout:\n{pull.stderr.strip()}",
                file=sys.stderr,
            )

    inject_git_exclude()
    return repo.resolve()


def inject_git_exclude() -> None:
    """Hide the clone dir from the host repo's git status; skip if cwd is not a git repo."""
    git_dir = Path.cwd() / ".git"
    if not git_dir.is_dir():
        return
    exclude = git_dir / "info" / "exclude"
    content = exclude.read_text() if exclude.exists() else ""
    if EXCLUDE_ENTRY in content.splitlines():
        return
    if content and not content.endswith("\n"):
        content += "\n"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(content + EXCLUDE_ENTRY + "\n")
