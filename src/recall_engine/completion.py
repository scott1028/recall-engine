"""Undo what Typer's `--install-completion` writes (bash/zsh/fish)."""

from __future__ import annotations

from pathlib import Path

PROG_NAME = "recall-engine"


def uninstall_completion() -> list[str]:
    """Remove this program's completion scripts; returns what was removed.

    Mirrors Typer's install paths. The rc lines Typer adds for zsh (fpath,
    compinit, zstyle) are shared shell setup, so only the per-program script and
    the bash `source` line are removed. PowerShell profiles are left alone.
    """
    home = Path.home()
    bash_script = home / ".bash_completions" / f"{PROG_NAME}.sh"
    scripts = [
        bash_script,
        home / ".zfunc" / f"_{PROG_NAME}",
        home / ".config" / "fish" / "completions" / f"{PROG_NAME}.fish",
    ]
    removed = []
    for script in scripts:
        if script.is_file():
            script.unlink()
            removed.append(str(script))
    bashrc = home / ".bashrc"
    if bashrc.is_file():
        source_line = f"source '{bash_script}'"
        lines = bashrc.read_text().splitlines(keepends=True)
        kept = [line for line in lines if line.strip() != source_line]
        if len(kept) != len(lines):
            bashrc.write_text("".join(kept))
            removed.append(f"{bashrc} (source line)")
    return removed
