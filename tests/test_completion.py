from typer.testing import CliRunner

from recall_engine.cli import app
from recall_engine.completion import PROG_NAME, uninstall_completion

runner = CliRunner()


def install_bash_completion(home) -> tuple:
    """Reproduce what Typer's --install-completion writes for bash."""
    script = home / ".bash_completions" / f"{PROG_NAME}.sh"
    script.parent.mkdir(parents=True)
    script.write_text("# completion script\n")
    bashrc = home / ".bashrc"
    bashrc.write_text(f"export FOO=1\n\nsource '{script}'\n")
    return script, bashrc


def test_uninstall_removes_script_and_bashrc_line(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    script, bashrc = install_bash_completion(tmp_path)
    zsh_script = tmp_path / ".zfunc" / f"_{PROG_NAME}"
    zsh_script.parent.mkdir()
    zsh_script.write_text("# zsh completion\n")

    removed = uninstall_completion()

    assert not script.exists()
    assert not zsh_script.exists()
    assert bashrc.read_text() == "export FOO=1\n\n"
    assert removed == [str(script), str(zsh_script), f"{bashrc} (source line)"]


def test_uninstall_without_installed_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".bashrc").write_text("export FOO=1\n")

    result = runner.invoke(app, ["--uninstall-completion"])

    assert result.exit_code == 0
    assert "no installed completion found" in result.output
    assert (tmp_path / ".bashrc").read_text() == "export FOO=1\n"


def test_uninstall_completion_flag_reports_removals(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    script, _ = install_bash_completion(tmp_path)

    result = runner.invoke(app, ["--uninstall-completion"])

    assert result.exit_code == 0
    assert f"removed {script}" in result.output
    assert not script.exists()
