import pytest
from typer.testing import CliRunner

from recall_engine import doctor
from recall_engine.cli import app
from recall_engine.doctor import run_doctor
from recall_engine.mcp_supervisor import ServerStatus

runner = CliRunner()
def install_passing_deps(monkeypatch) -> None:
    """Make every check pass without touching the real environment."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Never read the host's real /tmp state file: a wrap session running on the
    # dev machine would otherwise leak into the assertions.
    monkeypatch.setattr(doctor, "server_status", lambda: None)
def test_all_checks_pass(monkeypatch, tmp_path, capsys):
    install_passing_deps(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    assert run_doctor([str(tmp_path), str(other)]) is True
    output = capsys.readouterr().out
    for name in (
        "ugrep",
        "claude",
        "codex",
        "pi",
        "gemini",
        "opencode",
        "agy",
    ):
        assert f"[ok] {name}:" in output
    assert f"[ok] knowledge path: {tmp_path.resolve()}" in output
    assert f"[ok] knowledge path: {other.resolve()}" in output
    assert "[fail]" not in output
def test_single_agent_and_reachable_mcp_server_pass(monkeypatch, tmp_path, capsys):
    install_passing_deps(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"claude"} else None)
    monkeypatch.setattr(doctor, "server_status", lambda: ServerStatus(url="http://127.0.0.1:9/mcp", pid=123, owners=[1, 2], reachable=True))
    assert run_doctor([str(tmp_path)]) is True
    output = capsys.readouterr().out
    assert "[ok] claude:" in output
    assert "[skip] codex:" in output
    assert "reachable at http://127.0.0.1:9/mcp" in output
    assert "2 owner(s)" in output
    assert "[fail]" not in output
def test_missing_ugrep_is_skip_not_fail(monkeypatch, tmp_path, capsys):
    # ugrep is optional: search falls back to the built-in scan without it.
    install_passing_deps(monkeypatch)
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: None if name == "ugrep" else f"/usr/bin/{name}",
    )
    assert run_doctor([str(tmp_path)]) is True
    output = capsys.readouterr().out
    assert "[skip] ugrep: not found on PATH" in output
    assert "[fail]" not in output
def test_all_agents_missing_fails_and_exits_1(monkeypatch, tmp_path):
    install_passing_deps(monkeypatch)
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: None,
    )
    result = runner.invoke(app, ["doctor", "--knowledge-path", str(tmp_path)])
    assert result.exit_code == 1
    assert "[fail] agent CLIs" in result.output
    assert "claude/codex/pi/gemini/opencode/agy" in result.output
@pytest.mark.parametrize(
    ("case", "fail_name"),
    (("unset", "knowledge paths"), ("missing", "knowledge path")),
)
def test_required_doctor_checks_fail(monkeypatch, tmp_path, capsys, case, fail_name):
    install_passing_deps(monkeypatch)
    if case == "unset":
        knowledge_paths = None  # no --knowledge-path passed
    else:
        knowledge_paths = [str(tmp_path / "missing-dir")]
    assert run_doctor(knowledge_paths) is False
    output = capsys.readouterr().out
    assert f"[fail] {fail_name}:" in output
    if case == "unset":
        assert "pass --knowledge-path" in output
    if case == "missing":
        assert str(tmp_path / "missing-dir") in output
def test_no_running_mcp_server_is_skip_not_fail(monkeypatch, tmp_path, capsys):
    install_passing_deps(monkeypatch)
    assert run_doctor([str(tmp_path)]) is True
    output = capsys.readouterr().out
    assert "[skip] mcp server: not running (started on demand by `wrap`)" in output
    assert "[fail]" not in output
def test_stale_mcp_server_state_fails_with_unwrap_hint(monkeypatch, tmp_path, capsys):
    install_passing_deps(monkeypatch)
    status = ServerStatus(
        url="http://127.0.0.1:4321/mcp", pid=999, owners=[111], reachable=False
    )
    monkeypatch.setattr(doctor, "server_status", lambda: status)
    assert run_doctor([str(tmp_path)]) is False
    output = capsys.readouterr().out
    assert "[fail] mcp server" in output
    assert "stale state" in output
    assert "recall-engine unwrap" in output
