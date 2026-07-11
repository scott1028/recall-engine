import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
import pytest
from recall_engine.agents import AGENTS
from recall_engine.mcp_config import (
    McpConfigError,
    inject_mcp_config,
    restore_mcp_config,
)
URL = "http://127.0.0.1:8765/mcp"
TOKEN = "secret-token"
@pytest.fixture
def project(tmp_path, monkeypatch):
    """Run each test inside a fresh project cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
def marker_path(project: Path) -> Path:
    return project / ".agents" / "skills" / ".recall-engine-mcp-marker.json"
def config_path(project: Path, agent: str) -> Path:
    return project / AGENTS[agent].mcp.config_path
def dead_pid() -> int:
    """Spawn a short-lived process and return its pid after it exits."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid
def live_pid():
    """Spawn a long-lived process; caller must terminate it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
# --- 1. JSON config shapes per agent ----------------------------------------
def test_json_config_shapes_matrix(project):
    cases = [
        (
            "claude",
            ".mcp.json",
            "mcpServers",
            {
                "url": URL,
                "headers": None,
                "type": "http",
            },
        ),
        (
            "gemini",
            ".gemini/settings.json",
            "mcpServers",
            {
                "httpUrl": URL,
                "headers": None,
            },
        ),
        (
            "opencode",
            "opencode.json",
            "mcp",
            {
                "url": URL,
                "headers": None,
                "type": "remote",
                "enabled": True,
            },
        ),
        (
            "agy",
            ".agents/mcp_config.json",
            "mcpServers",
            {
                "serverUrl": URL,
                "headers": None,
            },
        ),
        (
            "pi",
            ".pi/mcp.json",
            "mcpServers",
            {
                "url": URL,
                "headers": None,
                "lifecycle": "keep-alive",
                "directTools": True,
            },
        ),
    ]
    for agent, relative_path, servers_key, expected_entry in cases:
        repo = project / f"repo-{agent}"
        inject_mcp_config(agent, repo, URL, token=TOKEN)
        path = config_path(project, agent)
        assert path == project / relative_path
        data = json.loads(path.read_text())
        entry = data[servers_key]["recall-engine"]
        assert entry == {
            **expected_entry,
            "headers": {
                "X-Recall-Repo": str(repo),
                "X-Recall-Token": TOKEN,
            },
        }
        assert restore_mcp_config(owner_pid=os.getpid()) is True
        assert not path.exists()
        assert not marker_path(project).exists()
# --- 2. TOML round-trip (codex) ---------------------------------------------
def test_codex_toml_roundtrip(project):
    repo = project / "repo"
    inject_mcp_config("codex", repo, URL, token=TOKEN)
    path = config_path(project, "codex")
    assert path == project / ".codex" / "config.toml"
    data = tomllib.loads(path.read_text())
    server = data["mcp_servers"]["recall-engine"]
    assert server["url"] == URL
    assert server["http_headers"] == {
        "X-Recall-Repo": str(repo),
        "X-Recall-Token": TOKEN,
    }
    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()
def test_codex_toml_preserves_user_content(project):
    repo = project / "repo"
    path = config_path(project, "codex")
    path.parent.mkdir(parents=True)
    original = '# my codex config\nmodel = "gpt-5"\n\n[tui]\ntheme = "dark"\n'
    path.write_text(original)
    inject_mcp_config("codex", repo, URL)
    data = tomllib.loads(path.read_text())
    # User content preserved alongside our server table.
    assert data["model"] == "gpt-5"
    assert data["tui"]["theme"] == "dark"
    assert data["mcp_servers"]["recall-engine"]["url"] == URL
    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert path.read_text() == original
# --- 3. Pre-existing JSON config backed up and fully restored ---------------
def test_preexisting_json_backed_up_and_restored(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    original = json.dumps({"mcpServers": {"other": {"url": "http://other"}}}, indent=2)
    path.write_text(original)
    inject_mcp_config("claude", repo, URL, token=TOKEN)
    data = json.loads(path.read_text())
    # Our entry added alongside the user's own server.
    assert data["mcpServers"]["other"] == {"url": "http://other"}
    assert data["mcpServers"]["recall-engine"]["url"] == URL
    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert path.read_text() == original  # byte-identical restore
    backup = Path(str(path) + ".recall-engine-mcp-backup")
    assert not backup.exists()
# --- 4. token=None omits X-Recall-Token -------------------------------------
def test_token_none_omits_token_header(project):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL)
    entry = json.loads(config_path(project, "claude").read_text())["mcpServers"][
        "recall-engine"
    ]
    assert entry["headers"] == {"X-Recall-Repo": str(repo)}
    assert "X-Recall-Token" not in entry["headers"]
def test_token_none_omits_token_header_toml(project):
    repo = project / "repo"
    inject_mcp_config("codex", repo, URL)
    server = tomllib.loads(config_path(project, "codex").read_text())["mcp_servers"][
        "recall-engine"
    ]
    assert server["http_headers"] == {"X-Recall-Repo": str(repo)}
# --- 5. Refcount across multiple owners -------------------------------------
def test_attach_same_repo_reasserts_entry_no_duplicate_backup(project):
    repo = project / "repo"
    inject_mcp_config("pi", repo, URL, token=TOKEN)  # first owner
    marker = marker_path(project)
    path = config_path(project, "pi")
    backup = Path(str(path) + ".recall-engine-mcp-backup")
    assert not backup.exists()  # nothing pre-existed -> no backup
    data = json.loads(path.read_text())
    del data["mcpServers"]["recall-engine"]["lifecycle"]
    path.write_text(json.dumps(data))
    other = live_pid()
    try:
        # Simulate another live wrap session owning the injection.
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        inject_mcp_config("pi", repo, URL, token=TOKEN)  # attach + re-assert
        record = json.loads(marker.read_text())
        pids = record["pids"]
        assert other.pid in pids and os.getpid() in pids
        assert len(record["configs"]) == 1
        assert not backup.exists()
        entry = json.loads(path.read_text())["mcpServers"]["recall-engine"]
        assert entry["lifecycle"] == "keep-alive"
    finally:
        other.terminate()
        other.wait()
def test_first_owner_leaving_keeps_config_then_last_restores(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    original = json.dumps({"mcpServers": {"other": {"url": "http://other"}}}, indent=2)
    path.write_text(original)
    inject_mcp_config("claude", repo, URL, token=TOKEN)  # backs up user config
    marker = marker_path(project)
    backup = Path(str(path) + ".recall-engine-mcp-backup")
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = sorted({os.getpid(), other.pid})
        marker.write_text(json.dumps(record))
        # First owner (us) leaves while `other` is alive -> keep injection.
        assert restore_mcp_config(owner_pid=os.getpid()) is True
        data = json.loads(path.read_text())
        assert "recall-engine" in data["mcpServers"]  # still injected
        assert backup.exists()
        assert json.loads(marker.read_text())["pids"] == [other.pid]
    finally:
        other.terminate()
        other.wait()
    # Last owner leaves (its pid now dead) -> full teardown restores user config.
    assert restore_mcp_config(owner_pid=other.pid) is True
    assert path.read_text() == original
    assert not marker.exists()
    assert not backup.exists()
def test_second_agent_attaches_with_its_own_config(project):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL, token=TOKEN)  # first owner, claude
    marker = marker_path(project)
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid, os.getpid()]
        marker.write_text(json.dumps(record))
        # A second agent (codex) wraps the same dir/repo -> its config is added.
        inject_mcp_config("codex", repo, URL, token=TOKEN)
        configs = json.loads(marker.read_text())["configs"]
        agents = {c["agent"] for c in configs}
        assert agents == {"claude", "codex"}
        assert config_path(project, "codex").exists()
    finally:
        other.terminate()
        other.wait()
    # Last owner teardown removes both config files.
    assert restore_mcp_config(force=True) is True
    assert not config_path(project, "claude").exists()
    assert not config_path(project, "codex").exists()
# --- 6. Different-repo attach refused ----------------------------------------
def test_attach_refused_when_repo_differs(project):
    inject_mcp_config("claude", project / "repo-a", URL)
    marker = marker_path(project)
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        with pytest.raises(McpConfigError, match="different"):
            inject_mcp_config("claude", project / "repo-b", URL)
    finally:
        other.terminate()
        other.wait()
# --- 7. Robust handling of pre-existing / malformed configs ------------------
def test_preexisting_jsonc_and_empty_configs_restore(project):
    cases = [
        ("gemini", '{\n  // my gemini settings\n  "theme": "dark"\n}\n'),
        ("claude", ""),
    ]
    for agent, original in cases:
        repo = project / f"repo-{agent}"
        path = config_path(project, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original)
        inject_mcp_config(agent, repo, URL, token=TOKEN)
        data = json.loads(path.read_text())
        if agent == "gemini":
            assert data["theme"] == "dark"
            assert data["mcpServers"]["recall-engine"]["httpUrl"] == URL
        else:
            assert data["mcpServers"]["recall-engine"]["url"] == URL
        assert restore_mcp_config(owner_pid=os.getpid()) is True
        assert path.read_text() == original
        assert not Path(str(path) + ".recall-engine-mcp-backup").exists()
def test_malformed_json_cases_preserve_file_and_do_not_backup(project):
    cases = [
        ('{"mcpServers": oops', "not valid JSON"),
        ("[]", "not a JSON object"),
    ]
    for original, error in cases:
        repo = project / f"repo-{len(original)}"
        path = config_path(project, "claude")
        path.write_text(original)
        with pytest.raises(McpConfigError, match=error):
            inject_mcp_config("claude", repo, URL)
        assert path.read_text() == original
        assert not marker_path(project).exists()
        assert not Path(str(path) + ".recall-engine-mcp-backup").exists()
def test_malformed_toml_raises_clean_error_and_preserves_file(project):
    repo = project / "repo"
    path = config_path(project, "codex")
    path.parent.mkdir(parents=True)
    original = 'model = "unterminated\n'
    path.write_text(original)
    with pytest.raises(McpConfigError, match="not valid TOML"):
        inject_mcp_config("codex", repo, URL)
    assert path.read_text() == original
    assert not Path(str(path) + ".recall-engine-mcp-backup").exists()
# --- Misc -------------------------------------------------------------------
def test_restore_with_nothing_returns_false(project):
    assert restore_mcp_config() is False
def test_stale_marker_all_dead_reinjected_fresh(project, capsys):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL, token=TOKEN)
    marker = marker_path(project)
    record = json.loads(marker.read_text())
    record["pids"] = [dead_pid()]
    marker.write_text(json.dumps(record))
    inject_mcp_config("claude", repo, URL, token=TOKEN)  # stale -> clean + fresh
    assert "stale wrap session" in capsys.readouterr().err
    assert json.loads(marker.read_text())["pids"] == [os.getpid()]
    entry = json.loads(config_path(project, "claude").read_text())["mcpServers"][
        "recall-engine"
    ]
    assert entry["url"] == URL
def test_no_mcp_spec_is_noop(project, monkeypatch):
    # An agent whose spec lacks .mcp must be a no-op (no marker, no error).
    monkeypatch.setitem(AGENTS, "nomcp", AGENTS["claude"].__class__(
        name="nomcp",
        skills_dir=".claude/skills",
        version_signature=None,
        install_hint="",
        mcp=None,
    ))
    inject_mcp_config("nomcp", project / "repo", URL)
    assert not marker_path(project).exists()
