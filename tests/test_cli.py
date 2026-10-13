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
        lambda token=None: ServerInfo(
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "claude-company"])
    assert result.exit_code == 7
    assert "launching claude-company..." in result.output
def test_wrap_pi_launches_without_adapter(monkeypatch, tmp_path):
    # pi >= 0.99 has built-in MCP, so wrap no longer requires pi-mcp-adapter.
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "pi"])
    assert result.exit_code == 0
    assert "launching pi..." in result.output
    # Injected .pi/mcp.json is cleaned up on exit.
    assert not (project / ".pi" / "mcp.json").exists()
def test_wrap_missing_knowledge_path_exits_2(tmp_path):
    missing = tmp_path / "missing"
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(missing), "claude"])
    assert result.exit_code == 2
    assert f"--knowledge-path points to a missing directory: {missing}" in result.output
def test_wrap_accepts_multiple_knowledge_paths(monkeypatch, tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    result = runner.invoke(
        app,
        [
            "wrap",
            "--knowledge-path",
            str(repo_a),
            "--knowledge-path",
            str(repo_b),
            "claude",
        ],
    )
    assert result.exit_code == 0
    assert f"knowledge path: {repo_a.resolve()}" in result.output
    assert f"knowledge path: {repo_b.resolve()}" in result.output
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), agent])
    assert result.exit_code == 7
    assert f"knowledge path: {repo.resolve()}" in result.output
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
    """Run `wrap claude` against a one-note knowledge dir; return the result."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "n.md").write_text("hello keyword\n")
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    return runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "claude"])
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
    # The agent's MCP config points at the shared server (with the paths header)
    # while it runs, and is cleaned up afterwards.
    repo = tmp_path / "repo"
    repo.mkdir()
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "claude"])
    assert result.exit_code == 0
    # The MCP config was present during the child run and pinned the paths header.
    injected = json.loads(probe.read_text())
    entry = injected["mcpServers"]["recall-engine"]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:9/mcp"
    assert entry["headers"]["X-Recall-Paths"] == json.dumps([str(repo.resolve())])
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "claude", "arg1", "arg2", "--resume"])
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "agy", "chat"])
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
    # A first live session already injected the skill for the SAME paths.
    from recall_engine.skill import inject_skill
    inject_skill([repo])
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "claude"])
        assert result.exit_code == 0            # attached, not refused
        assert marker.exists()                  # other session survives
        assert other.pid in json.loads(marker.read_text())["pids"]
    finally:
        other.terminate()
        other.wait()
def test_wrap_auto_detects_paths_from_live_session(monkeypatch, tmp_path):
    # Second wrap in the same project inherits the running session's paths
    # without passing --knowledge-path.
    import subprocess
    import sys
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    from recall_engine.skill import inject_skill
    inject_skill([repo])  # a first session set up the injection for `repo`
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        # No --knowledge-path: the wrapper must auto-detect `repo` and attach.
        result = runner.invoke(app, ["wrap", "claude"])
        assert result.exit_code == 0
        assert f"knowledge path: {repo.resolve()}" in result.output
        assert other.pid in json.loads(marker.read_text())["pids"]
    finally:
        other.terminate()
        other.wait()
def test_wrap_without_config_and_no_session_exits_2(monkeypatch, tmp_path):
    # No --knowledge-path and no live session -> the config error fires.
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 2
    assert "pass --knowledge-path" in result.output
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
    result = runner.invoke(app, ["wrap", "--knowledge-path", str(repo), "claude"])
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
def test_options_passed_before_the_command_reach_it(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "recall_engine.cli.run_doctor",
        lambda paths: bool(captured.update(paths=paths)) or True,
    )
    result = runner.invoke(
        app,
        [
            "--knowledge-path",
            str(tmp_path),
            "--knowledge-path",
            str(tmp_path / "nested"),
            "doctor",
        ],
    )
    assert result.exit_code == 0
    assert captured == {
        "paths": [str(tmp_path), str(tmp_path / "nested")],
    }
def test_command_options_win_over_the_ones_before_the_command(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "recall_engine.cli.run_doctor",
        lambda paths: bool(captured.update(paths=paths)) or True,
    )
    result = runner.invoke(
        app,
        [
            "--knowledge-path",
            "/global/repo",
            "doctor",
            "--knowledge-path",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert captured["paths"] == [str(tmp_path)]


@pytest.mark.parametrize("agent", ("claude", "agy"))
def test_wrap_web_serves_agent_and_restores_config(monkeypatch, tmp_path, agent):
    # `wrap --web` hands the agent command to the web server instead of
    # launching it, and tears down the injected config when the server stops.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0", name=agent)
    monkeypatch.chdir(project)
    captured = {}

    def fake_run_web(command, env, host, port, api_key):
        captured.update(command=command, env=env, host=host, port=port, api_key=api_key)
        captured["skill_present"] = (
            project / ".agents" / "skills" / "recall-engine" / "SKILL.md"
        ).exists()
        return 0

    monkeypatch.setattr("recall_engine.cli.run_web", fake_run_web)
    monkeypatch.setattr(
        "recall_engine.cli.launch_agent",
        lambda *a, **k: pytest.fail("launch_agent must not run with --web"),
    )
    result = runner.invoke(
        app,
        [
            "wrap",
            "--web",
            "--web-writable",
            "--web-host",
            "0.0.0.0",
            "--web-port",
            "9999",
            "--knowledge-path",
            str(repo),
            agent,
            "chat",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["api_key"]
    assert f"/?api-key={captured['api_key']}" in result.output
    assert captured["env"]["RECALL_REPO_PATH"] == str(repo.resolve())
    assert captured["skill_present"]
    command = " ".join(captured["command"])
    assert command.endswith("chat")
    if agent == "agy":
        assert f"--add-dir {Path.cwd()}" in command
    # Injection is torn down after the server stops.
    assert not (project / ".agents" / "skills" / "recall-engine").exists()
    assert not (project / ".mcp.json").exists()



@pytest.mark.parametrize(
    ("api_key_args", "printed"),
    (
        (["--api-key", "my-key"], "/?api-key=my-key\n"),
        # Special characters are URL-encoded so the printed URL still works.
        (["--api-key", "a&b#c=d e"], "/?api-key=a%26b%23c%3Dd%20e\n"),
    ),
)
def test_wrap_web_uses_given_api_key(monkeypatch, tmp_path, api_key_args, printed):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    captured = {}
    monkeypatch.setattr(
        "recall_engine.cli.run_web",
        lambda command, env, host, port, api_key: captured.update(api_key=api_key)
        or 0,
    )
    result = runner.invoke(
        app,
        ["wrap", "--web", "--web-writable", *api_key_args, "--knowledge-path", str(repo), "claude"],
    )
    assert result.exit_code == 0, result.output
    assert captured["api_key"] == api_key_args[1]
    assert printed in result.output


def test_wrap_web_generates_random_api_key(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    keys = []
    monkeypatch.setattr(
        "recall_engine.cli.run_web",
        lambda command, env, host, port, api_key: keys.append(api_key) or 0,
    )
    outputs = [
        runner.invoke(
            app,
            ["wrap", "--web", "--web-writable", "--knowledge-path", str(repo), "claude"],
        ).output
        for _ in range(2)
    ]
    assert len(keys[0]) >= 32 and keys[0] != keys[1]
    for key, output in zip(keys, outputs):
        assert f"/?api-key={key}\n" in output


def run_wrap_web(monkeypatch, tmp_path, *extra_args):
    """Run `wrap --web [extra_args] claude chat` with run_web captured."""
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.chdir(project)
    captured = {}
    monkeypatch.setattr(
        "recall_engine.cli.run_web",
        lambda command, env, host, port, api_key: captured.update(command=command)
        or 0,
    )
    result = runner.invoke(
        app,
        ["wrap", "--web", *extra_args, "--knowledge-path", str(repo), "claude", "chat"],
    )
    return result, captured, project


def test_wrap_web_is_read_only_by_default(monkeypatch, tmp_path):
    # The sandbox probe needs a working bwrap; this test only checks the command.
    monkeypatch.setattr("recall_engine.cli.check_sandbox", lambda prefix: None)
    result, captured, project = run_wrap_web(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    command = captured["command"]
    assert Path(command[0]).name == "bwrap"
    agent_part = " ".join(command[command.index("--") + 1 :])
    assert "--disallowedTools Edit,Write,NotebookEdit chat" in agent_part
    assert f"web terminal: read-only (writable: {project.resolve() / '.tmp'})" in result.output


def test_wrap_web_writable_skips_the_sandbox(monkeypatch, tmp_path):
    result, captured, _ = run_wrap_web(monkeypatch, tmp_path, "--web-writable")
    assert result.exit_code == 0, result.output
    command = " ".join(captured["command"])
    assert "bwrap" not in command
    assert "--disallowedTools" not in command
    assert "read-only" not in result.output


def test_wrap_web_without_bwrap_exits_1_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr("recall_engine.web_terminal.shutil.which", lambda name: None)
    result, captured, project = run_wrap_web(monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "bubblewrap" in result.output
    assert "--web-writable" in result.output
    assert not captured
    assert not (project / ".agents" / "skills" / "recall-engine").exists()
    assert not (project / ".mcp.json").exists()

def test_wrap_web_sigterm_restores_config(monkeypatch, tmp_path):
    # uvicorn re-raises a captured SIGTERM after shutdown; wrap must still reach
    # its finally and restore the injected config. Run the real CLI in a
    # subprocess so a regression fails cleanly instead of killing pytest.
    import signal
    import socket
    import subprocess
    import sys
    import time

    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "--version" ]; then echo "2.0.0 (Claude Code)"; fi\nexit 0',
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Stub the shared MCP server, as the autouse fixture does in-process.
    script = (
        "import os, sys\n"
        "import recall_engine.cli as cli\n"
        "from recall_engine.mcp_supervisor import ServerInfo\n"
        "cli.ensure_server = lambda token=None: ServerInfo("
        "url='http://127.0.0.1:9/mcp', port=9, token='t', pid=os.getpid())\n"
        "cli.release_server = lambda owner_pid=None, force=False: False\n"
        "cli.app(sys.argv[1:])\n"
    )
    runner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            "wrap",
            "--web",
            "--web-writable",
            "--web-port",
            str(port),
            "--knowledge-path",
            str(repo),
            "claude",
        ],
        cwd=project,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        for line in runner.stdout:
            if line.startswith("web terminal:"):
                break
        # Give uvicorn time to install its signal handlers and bind.
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        assert (project / ".mcp.json").exists()
        runner.send_signal(signal.SIGTERM)
        returncode = runner.wait(timeout=15)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
    assert returncode == 0
    assert not (project / ".agents" / "skills" / "recall-engine").exists()
    assert not (project / ".mcp.json").exists()
