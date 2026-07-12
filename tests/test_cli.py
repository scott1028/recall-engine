import json
import os
from pathlib import Path
import pytest
from typer.testing import CliRunner
from recall_engine import search
from recall_engine.cli import app
from recall_engine.mcp_supervisor import ServerInfo
runner = CliRunner()
@pytest.fixture(autouse=True)
def stub_mcp_server(monkeypatch):
    """Keep `wrap`/`unwrap` from spawning a real server or touching the global
    /tmp state file. The per-project MCP-config injection still runs for real."""
    monkeypatch.setattr(
        "recall_engine.cli.ensure_server",
        lambda repo=None, token=None: ServerInfo(
            url="http://127.0.0.1:9/mcp", port=9, token="testtok", pid=os.getpid()
        ),
    )
    monkeypatch.setattr(
        "recall_engine.cli.release_server",
        lambda owner_pid=None, force=False: False,
    )
def install_fake_claude(tmp_path, monkeypatch, script: str, name: str = "claude") -> None:
    """Put a fake claude shell script on PATH."""
    # Point HOME/SHELL at the sandbox so the real ~/.bashrc is not sourced.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
def test_wrap_rejects_unknown_agent(monkeypatch, tmp_path):
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'echo "some-other-tool 1.0"',
        name="notclaude",
    )
    result = runner.invoke(app, ["wrap", "notclaude"])
    assert result.exit_code == 2
    assert "does not look like a supported agent CLI" in result.output
def test_wrap_rejects_missing_agent(monkeypatch, tmp_path):
    # Agent command not on PATH at all -> detection fails.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = runner.invoke(app, ["wrap", "no-such-agent"])
    assert result.exit_code == 2
    assert "does not look like a supported agent CLI" in result.output
def test_wrap_detects_claude_wrapper(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    # A wrapper named claude-company that reports itself as Claude Code.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "--version" ]; then echo "2.0.0 (Claude Code)"; exit 0; fi\n'
        "exit 7",
        name="claude-company",
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude-company"])
    assert result.exit_code == 7
    assert "launching claude-company..." in result.output
def test_wrap_pi_blocks_without_adapter(monkeypatch, tmp_path):
    # pi runs but lacks pi-mcp-adapter -> wrap refuses before any injection.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "list" ]; then echo "  npm:pi-web-access"; exit 0; fi\nexit 0',
        name="pi",
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "pi"])
    assert result.exit_code == 2
    assert "pi install npm:pi-mcp-adapter" in result.output
    # Blocked before setup: nothing injected, nothing launched.
    assert "launching pi..." not in result.output
    assert not (project / ".pi" / "mcp.json").exists()
def test_wrap_pi_launches_with_adapter(monkeypatch, tmp_path):
    # pi reports the adapter -> wrap proceeds through the full lifecycle.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "list" ]; then echo "  npm:pi-mcp-adapter"; exit 0; fi\nexit 0',
        name="pi",
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "pi"])
    assert result.exit_code == 0
    assert "launching pi..." in result.output
    # Injected .pi/mcp.json is cleaned up on exit.
    assert not (project / ".pi" / "mcp.json").exists()
def test_wrap_claude_repo_error_exits_1(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(missing), "claude"])
    assert result.exit_code == 1
    assert f"--local-knowledge-path points to a missing directory: {missing}" in result.output
@pytest.mark.parametrize(
    ("agent", "link_dir"),
    (
        ("claude", ".claude"),
        ("gemini", ".gemini"),
        ("opencode", ".opencode"),
        # agy reads .agents/skills directly, so it gets no symlink of its own.
        ("agy", None),
    ),
)
def test_wrap_full_lifecycle(monkeypatch, tmp_path, agent, link_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "recall-engine"
    link = project / link_dir / "skills" / "recall-engine" if link_dir else None
    probe = tmp_path / "probe.txt"
    # The fake agent records that the injected skill was reachable while it ran
    # (at the SSOT, and through its own symlink when it has one), then exits 7 so
    # the wrapper's exit-code passthrough is covered too.
    reachable = f'[ -f "{skill_dir / "SKILL.md"}" ]'
    if link is not None:
        reachable += f' && [ -f "{link / "SKILL.md"}" ]'
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'if {reachable}; then echo present > "{probe}"; fi\nexit 7',
        name=agent,
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), agent])
    assert result.exit_code == 7
    assert f"knowledge repo: {repo.resolve()}" in result.output
    assert f"launching {agent}..." in result.output
    assert probe.read_text().strip() == "present"
    # Everything injected is torn down when the session ends.
    assert not skill_dir.exists()
    assert not (
        project / ".agents" / "skills" / ".recall-engine-marker.json"
    ).exists()
    if link is not None:
        assert not link.is_symlink()
    else:
        assert not (project / ".agy").exists()
def wrap_for_search_backend(monkeypatch, tmp_path):
    """Run `wrap claude` against a one-note repo; return the CliRunner result."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "n.md").write_text("hello keyword\n")
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    return runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude"])
@pytest.mark.skipif(search.ugrep_path() is None, reason="ugrep not installed")
def test_wrap_reports_ugrep(monkeypatch, tmp_path):
    result = wrap_for_search_backend(monkeypatch, tmp_path)
    assert result.exit_code == 0
    assert f"knowledge search: ugrep ({search.ugrep_path()})" in result.output
def test_wrap_warns_when_ugrep_missing(monkeypatch, tmp_path):
    # Without ugrep wrap still launches; it only warns that search is on the
    # slow path and tells the user how to install it.
    monkeypatch.setattr("recall_engine.search.ugrep_path", lambda: None)
    result = wrap_for_search_backend(monkeypatch, tmp_path)
    assert result.exit_code == 0
    assert "knowledge search: ugrep not found on PATH" in result.output
    assert search.UGREP_INSTALL_HINT in result.output
    assert "launching claude..." in result.output


def test_wrap_injects_and_restores_mcp_config(monkeypatch, tmp_path):
    # The agent's MCP config points at the shared server (with the repo header)
    # while it runs, and is cleaned up afterwards.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    mcp_json = project / ".mcp.json"
    probe = tmp_path / "probe.txt"
    # Fake claude records the injected .mcp.json contents while it runs.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'cat "{mcp_json}" > "{probe}"\nexit 0',
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude"])
    assert result.exit_code == 0
    # The MCP config was present during the child run and pinned the repo header.
    injected = json.loads(probe.read_text())
    entry = injected["mcpServers"]["recall-engine"]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:9/mcp"
    assert entry["headers"]["X-Recall-Repo"] == str(repo.resolve())
    # ...and it is cleaned up afterwards.
    assert not mcp_json.exists()
def test_wrap_forwards_extra_args_to_agent(monkeypatch, tmp_path):
    # `wrap claude arg1 arg2` must reach claude as `claude arg1 arg2`.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    out = tmp_path / "args.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{out}"\nexit 0')
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude", "arg1", "arg2", "--resume"])
    assert result.exit_code == 0
    assert out.read_text().strip() == "arg1 arg2 --resume"
def test_wrap_agy_launched_with_add_dir(monkeypatch, tmp_path):
    # agy only reads the injected .agents/ config when the project dir is in its
    # workspace, so wrap must launch it with `--add-dir <project>`.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    out = tmp_path / "args.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{out}"\nexit 0', name="agy")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "agy", "chat"])
    assert result.exit_code == 0
    parts = out.read_text().split()
    assert parts[0] == "--add-dir"
    assert Path(parts[1]).resolve() == project.resolve()
    assert parts[2] == "chat"  # user args still forwarded, after the pre-args
def test_wrap_claude_attaches_to_live_session(monkeypatch, tmp_path):
    import subprocess
    import sys
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    # A first live session already injected the skill for the SAME repo.
    from recall_engine.skill import inject_skill
    inject_skill(repo)
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude"])
        assert result.exit_code == 0            # attached, not refused
        assert marker.exists()                  # other session survives
        assert other.pid in json.loads(marker.read_text())["pids"]
    finally:
        other.terminate()
        other.wait()
def test_wrap_auto_detects_repo_from_live_session(monkeypatch, tmp_path):
    # Second wrap in the same project inherits the running session's repo
    # without passing --local-knowledge-path.
    import subprocess
    import sys
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    from recall_engine.skill import inject_skill
    inject_skill(repo)  # a first session set up the injection for `repo`
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        # No --local-knowledge-path: the wrapper must auto-detect `repo` and attach.
        result = runner.invoke(app, ["wrap", "claude"])
        assert result.exit_code == 0
        assert f"knowledge repo: {repo.resolve()}" in result.output
        assert other.pid in json.loads(marker.read_text())["pids"]
    finally:
        other.terminate()
        other.wait()
def test_wrap_without_config_and_no_session_exits_2(monkeypatch, tmp_path):
    # No --local-knowledge-path and no live session -> the config error fires.
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 2
    assert "pass --local-knowledge-path" in result.output
def test_wrap_claude_missing_claude_restores_and_exits_1(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude"])
    assert result.exit_code == 1
    # Injection was rolled back on the launcher error path.
    assert not (project / ".agents" / "skills" / "recall-engine").exists()
    assert not (
        project / ".agents" / "skills" / ".recall-engine-marker.json"
    ).exists()
    assert not (
        project / ".claude" / "skills" / "recall-engine"
    ).is_symlink()
def test_unwrap_cleans_stale_state_and_reports_empty_project(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    skill_dir = project / ".agents" / "skills" / "recall-engine"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("leftover\n")
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    marker.write_text(json.dumps({"pid": 1, "backup": None}))
    result = runner.invoke(app, ["unwrap"])
    assert result.exit_code == 0
    assert "restored leftover skill and MCP state" in result.output
    assert not skill_dir.exists()
    assert not marker.exists()
    empty_project = tmp_path / "empty-project"
    empty_project.mkdir()
    monkeypatch.chdir(empty_project)
    result = runner.invoke(app, ["unwrap"])
    assert result.exit_code == 0
    assert "nothing to clean" in result.output
def _stub_repo_first(monkeypatch, repo_first: bool):
    monkeypatch.setattr(
        "recall_engine.cli.ensure_server",
        lambda repo=None, token=None: ServerInfo(
            url="http://127.0.0.1:9/mcp",
            port=9,
            token="testtok",
            pid=os.getpid(),
            repo_first=repo_first,
        ),
    )
def test_wrap_first_repo_triggers_background_download(monkeypatch, tmp_path):
    # First wrap of a repo (repo_first) + a Drive folder -> background sync.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    _stub_repo_first(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        "recall_engine.cli._spawn_background_download",
        lambda repo, drive_folder: calls.append((repo, drive_folder)),
    )
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "--remote-knowledge-folder", "Shared", "claude"])
    assert result.exit_code == 0
    assert calls == [(repo.resolve(), "Shared")]
    assert "drive sync: downloading" in result.output
def test_wrap_non_first_repo_skips_background_download(monkeypatch, tmp_path):
    # repo_first is False -> no auto-sync even with a Drive folder set.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    _stub_repo_first(monkeypatch, False)
    calls = []
    monkeypatch.setattr(
        "recall_engine.cli._spawn_background_download",
        lambda repo, drive_folder: calls.append((repo, drive_folder)),
    )
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "--remote-knowledge-folder", "Shared", "claude"])
    assert result.exit_code == 0
    assert calls == []
    assert "drive sync:" not in result.output  # attach -> stay silent
def test_wrap_first_repo_without_drive_folder_skips_download(monkeypatch, tmp_path):
    # repo_first is True but no Drive folder configured -> nothing to download.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    _stub_repo_first(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        "recall_engine.cli._spawn_background_download",
        lambda repo, drive_folder: calls.append((repo, drive_folder)),
    )
    result = runner.invoke(app, ["wrap", "--local-knowledge-path", str(repo), "claude"])
    assert result.exit_code == 0
    assert calls == []
    assert "drive sync: skipped" in result.output
def test_spawn_background_download_command(monkeypatch, tmp_path):
    # The child gets its config on the command line, not through the environment.
    import subprocess
    import sys
    from recall_engine import cli
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "log_path", lambda: tmp_path / "mcp.log")
    recorded = {}
    def fake_popen(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return object()
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    cli._spawn_background_download(repo, "Shared")
    assert recorded["argv"] == [
        sys.executable, "-m", "recall_engine", "sync", "download",
        "--local-knowledge-path", str(repo),
        "--remote-knowledge-folder", "Shared",
    ]
    assert "env" not in recorded["kwargs"]
    assert recorded["kwargs"]["start_new_session"] is True
    assert recorded["kwargs"]["stdout"] == subprocess.DEVNULL
    assert recorded["kwargs"]["stdin"] == subprocess.DEVNULL
def test_spawn_background_download_swallows_errors(monkeypatch, tmp_path):
    from recall_engine import cli
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "log_path", lambda: tmp_path / "mcp.log")
    def boom(*args, **kwargs):
        raise OSError("cannot spawn")
    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    # Must not raise.
    cli._spawn_background_download(repo, "Shared")
def test_options_passed_before_the_command_reach_it(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "recall_engine.cli.run_doctor",
        lambda local, remote: bool(captured.update(local=local, remote=remote)) or True,
    )
    result = runner.invoke(
        app,
        [
            "--local-knowledge-path",
            str(tmp_path),
            "--remote-knowledge-folder",
            "Shared",
            "doctor",
        ],
    )
    assert result.exit_code == 0
    assert captured == {"local": str(tmp_path), "remote": "Shared"}
def test_command_options_win_over_the_ones_before_the_command(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "recall_engine.cli.run_doctor",
        lambda local, remote: bool(captured.update(local=local, remote=remote)) or True,
    )
    result = runner.invoke(
        app,
        [
            "--local-knowledge-path",
            "/global/repo",
            "doctor",
            "--local-knowledge-path",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert captured["local"] == str(tmp_path)
