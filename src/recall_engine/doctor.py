"""Environment diagnostics: one line per check, with actionable fixes."""

from __future__ import annotations

import shutil
from pathlib import Path

from recall_engine import search
from recall_engine.agents import AGENTS
from recall_engine.mcp_supervisor import server_status


def _ok(name: str, detail: str) -> None:
    print(f"[ok] {name}: {detail}")


def _fail(name: str, detail: str, fix: str) -> None:
    print(f"[fail] {name}: {detail}")
    for line in fix.splitlines():
        print(f"    {line}")


def _check_agents() -> bool:
    """Report each supported agent CLI; pass if at least one is installed."""
    found = False
    for name in AGENTS:
        path = shutil.which(name)
        if path is None:
            print(f"[skip] {name}: not found on PATH")
        else:
            _ok(name, path)
            found = True
    if not found:
        _fail(
            "agent CLIs",
            f"none of {'/'.join(AGENTS)} found on PATH",
            "\n".join(spec.install_hint for spec in AGENTS.values()),
        )
    return found


def _check_mcp() -> bool:
    """The shared knowledge MCP server needs the `mcp` package importable."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        _fail(
            "mcp package",
            "python 'mcp' package not importable",
            "reinstall recall-engine so its dependencies are present",
        )
        return False
    _ok("mcp package", "streamable-HTTP MCP server available")
    return True


def _check_mcp_server() -> bool:
    """Reachability of the shared server. Not running is normal: `wrap` starts
    it on demand. A recorded but unreachable server means stale state.
    """
    status = server_status()
    if status is None:
        print("[skip] mcp server: not running (started on demand by `wrap`)")
        return True
    if status.reachable:
        _ok(
            "mcp server",
            f"reachable at {status.url} (pid {status.pid}, {len(status.owners)} owner(s))",
        )
        return True
    _fail(
        "mcp server",
        f"recorded at {status.url} but not reachable (stale state)",
        "run `recall-engine unwrap` to clear it; the next `wrap` respawns the server",
    )
    return False


def _report_ugrep() -> None:
    """Informational: ugrep only speeds up the knowledge search; the built-in
    scan covers the same searches without it, so a miss is not a failure."""
    path = search.ugrep_path()
    if path is None:
        print(
            "[skip] ugrep: not found on PATH; knowledge search falls back to a "
            f"slower built-in scan ({search.UGREP_INSTALL_HINT})"
        )
        return
    _ok("ugrep", path)


def _report_pi_trust() -> None:
    """Informational: pi (>= 0.99, built-in MCP) reads a project's .pi/mcp.json
    only for trusted projects, so the injected server stays invisible until the
    project is trusted (the first `pi` run in it prompts to trust)."""
    if shutil.which("pi") is None:
        return
    print(
        "[note] pi: needs pi >= 0.99 (built-in MCP) and reads the injected "
        ".pi/mcp.json only in trusted projects; if the recall-engine tools do "
        "not appear, run pi once in the project and accept the trust prompt"
    )


def _report_codex_trust() -> None:
    """Informational: codex reads a project's .codex/config.toml only for
    trusted projects, so the injected server stays invisible until the project
    is trusted (the first `codex` run in it prompts to trust)."""
    if shutil.which("codex") is None:
        return
    print(
        "[note] codex: reads the injected .codex/config.toml only in trusted "
        "projects; if the recall-engine tools do not appear, run codex once in "
        "the project and accept the trust prompt"
    )


def _check_knowledge_paths(knowledge_paths: list[str] | None) -> bool:
    if not knowledge_paths:
        _fail(
            "knowledge paths",
            "not configured",
            "pass --knowledge-path for each existing knowledge directory",
        )
        return False
    passed = True
    for raw in knowledge_paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            _ok("knowledge path", str(path.resolve()))
        else:
            _fail(
                "knowledge path",
                f"{path} is not an existing directory",
                "pass --knowledge-path for each existing knowledge directory",
            )
            passed = False
    return passed


def run_doctor(knowledge_paths: list[str] | None = None) -> bool:
    """Run every check; return True only when all required checks pass."""
    results = [
        _check_agents(),
        _check_mcp(),
        _check_mcp_server(),
        _check_knowledge_paths(knowledge_paths),
    ]
    _report_ugrep()
    _report_pi_trust()
    _report_codex_trust()
    return all(results)
