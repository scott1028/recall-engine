"""Typer app; command dispatch only."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from pathlib import Path
from typing import Annotated

import typer

from recall_engine import search
from recall_engine.agents import AGENTS
from recall_engine.completion import uninstall_completion
from recall_engine.config import ConfigError, resolve_settings
from recall_engine.doctor import run_doctor
from recall_engine.launcher import (
    LauncherError,
    build_agent_command,
    detect_agent,
    launch_agent,
)
from recall_engine.mcp_config import (
    McpConfigError,
    inject_mcp_config,
    restore_mcp_config,
)
from recall_engine.mcp_server import run_server
from recall_engine.mcp_supervisor import (
    SupervisorError,
    ensure_server,
    release_server,
)
from recall_engine.skill import (
    SkillError,
    detect_active_knowledge_paths,
    inject_skill,
    restore_skill,
)
from recall_engine.web_terminal import run_web

KNOWLEDGE_PATH_HELP = (
    "Path to an existing local knowledge directory; its notes are found under "
    "<path>/**/*.md. Repeat the option for multiple directories. Optional for a "
    "wrap in a directory that already has a running session, where the paths "
    "are auto-detected."
)

app = typer.Typer(
    no_args_is_help=True,
    help="""Recall Engine: wrap an agent CLI (claude/codex/pi/gemini/opencode/agy) with one or more local knowledge directories searched through a shared MCP server.

--knowledge-path also works on `wrap` and `doctor`, where it overrides the values given here. With `wrap`, pass it before AGENT, since everything after AGENT goes to the agent CLI.

Search backend:

The knowledge search (`search_knowledge`) runs `ugrep` when it is on PATH. Without it the search still works, on a slower built-in scan; install ugrep with `sudo apt install ugrep` or `brew install ugrep`.""",
)


def _uninstall_completion_callback(ctx: typer.Context, value: bool) -> bool:
    if not value or ctx.resilient_parsing:
        return value
    removed = uninstall_completion()
    if not removed:
        typer.echo("no installed completion found")
        raise typer.Exit()
    for path in removed:
        typer.echo(f"removed {path}")
    typer.echo("Completion will stop once you restart the terminal")
    raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    knowledge_path: Annotated[
        list[str] | None, typer.Option("--knowledge-path", help=KNOWLEDGE_PATH_HELP)
    ] = None,
    uninstall_completion_flag: bool = typer.Option(
        None,
        "--uninstall-completion",
        callback=_uninstall_completion_callback,
        expose_value=False,
        help="Uninstall completion for the current shell (bash/zsh/fish; "
        "PowerShell profiles are left alone).",
    ),
) -> None:
    ctx.obj = {"knowledge_path": knowledge_path}


def _resolve_options(ctx: typer.Context, knowledge_path: list[str]) -> list[str]:
    """Command-level options win; fall back to the ones passed before the command."""
    global_options = ctx.obj or {}
    return knowledge_path or global_options.get("knowledge_path") or []


@app.command(
    context_settings={
        # Everything after AGENT is forwarded verbatim to the agent CLI, so
        # `wrap claude foo --bar` behaves like `claude foo --bar` (env vars are
        # inherited automatically).
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
)
def wrap(
    ctx: typer.Context,
    agent: str,
    knowledge_path: Annotated[
        list[str] | None, typer.Option("--knowledge-path", help=KNOWLEDGE_PATH_HELP)
    ] = None,
    web: Annotated[
        bool,
        typer.Option(
            "--web",
            help="Serve AGENT in a browser terminal instead of this one; each "
            "browser connection runs its own agent.",
        ),
    ] = False,
    web_host: Annotated[
        str, typer.Option("--web-host", help="Bind address for --web.")
    ] = "127.0.0.1",
    web_port: Annotated[
        int, typer.Option("--web-port", help="Port for --web.")
    ] = 7681,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            help="API key the --web browser terminal requires; defaults to a "
            "random key.",
        ),
    ] = None,
) -> None:
    """Prepare the knowledge paths and launch AGENT (claude/codex/pi/gemini/opencode/agy or a wrapper).

    Extra arguments after AGENT are passed straight through to it, so pass these
    options before AGENT.
    """
    agent_args = list(ctx.args)
    knowledge_path = _resolve_options(ctx, knowledge_path)
    # Known names are trusted as-is; other names must classify as a wrapper
    # of one of the supported agent families.
    family = agent if agent in AGENTS else detect_agent(agent)
    if family is None:
        typer.echo(
            f"'{agent}' does not look like a supported agent CLI "
            f"({'/'.join(AGENTS)}); `{agent} --version` did not match any "
            "known agent.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        # A live wrap session in this dir lets a second wrap reuse its paths
        # without --knowledge-path; the option still wins when passed.
        settings = resolve_settings(
            knowledge_paths=knowledge_path,
            fallback_paths=detect_active_knowledge_paths(),
        )
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    for path in settings.knowledge_paths:
        typer.echo(f"knowledge path: {path}")
    # Search works either way; tell the user when it is on the slow path.
    ugrep = search.ugrep_path()
    if ugrep is None:
        typer.echo(
            "knowledge search: ugrep not found on PATH; using the slower "
            f"built-in scan ({search.UGREP_INSTALL_HINT})",
            err=True,
        )
    else:
        typer.echo(f"knowledge search: ugrep ({ugrep})")
    pid = os.getpid()
    # Inject the skill (the reliable "search first" trigger), start/reuse the
    # shared MCP server, and point this agent's config at it. All three are torn
    # down in the finally, and each restore is idempotent and safe to call even
    # when its setup step did not run.
    try:
        knowledge_paths = list(settings.knowledge_paths)
        inject_skill(knowledge_paths)
        server = ensure_server()
        inject_mcp_config(family, knowledge_paths, server.url, server.token)
        typer.echo(f"launching {agent}...")
        # agy only reads the injected .agents/ config when the project dir is in
        # its workspace, so pass `--add-dir <project>` for it.
        pre_args = (
            ["--add-dir", str(Path.cwd())]
            if AGENTS[family].needs_workspace_dir
            else []
        )
        if web:
            command, env = build_agent_command(
                knowledge_paths[0],
                argv=agent_args,
                agent=agent,
                install_hint=AGENTS[family].install_hint,
                pre_args=pre_args,
            )
            api_key = api_key or secrets.token_urlsafe(24)
            typer.echo(
                f"web terminal: http://{web_host}:{web_port}/"
                f"?api-key={urllib.parse.quote(api_key, safe='')}"
            )
            exit_code = run_web(command, env, web_host, web_port, api_key)
        else:
            exit_code = launch_agent(
                knowledge_paths[0],
                argv=agent_args,
                agent=agent,
                install_hint=AGENTS[family].install_hint,
                pre_args=pre_args,
            )
    except (SkillError, SupervisorError, McpConfigError, LauncherError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        restore_mcp_config(owner_pid=pid)
        restore_skill(owner_pid=pid)
        release_server(owner_pid=pid)
    raise typer.Exit(exit_code)


@app.command()
def unwrap() -> None:
    """Clean up leftovers from an aborted wrap session."""
    # Skill and MCP-config state are per-project, so force-clean them here.
    cleaned = restore_mcp_config(force=True)
    cleaned = restore_skill(force=True) or cleaned
    # The MCP server is machine-global; only stop it when no live owner remains
    # (never force-kill — other projects may still be using the shared server).
    release_server(owner_pid=os.getpid())
    if cleaned:
        typer.echo("restored leftover skill and MCP state")
    else:
        typer.echo("nothing to clean")


@app.command()
def doctor(
    ctx: typer.Context,
    knowledge_path: Annotated[
        list[str] | None, typer.Option("--knowledge-path", help=KNOWLEDGE_PATH_HELP)
    ] = None,
) -> None:
    """Diagnose knowledge path config and agent CLIs on PATH."""
    knowledge_path = _resolve_options(ctx, knowledge_path)
    if not run_doctor(knowledge_path):
        raise typer.Exit(1)


@app.command(name="mcp-serve", hidden=True)
def mcp_serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
) -> None:
    """Run the shared knowledge MCP server (spawned by `wrap`; internal use)."""
    run_server(host, port, token or None)
