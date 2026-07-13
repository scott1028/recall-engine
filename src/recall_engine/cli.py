"""Typer app; command dispatch only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

from recall_engine import search
from recall_engine.agents import AGENTS
from recall_engine.completion import uninstall_completion
from recall_engine.config import ConfigError, resolve_settings
from recall_engine.doctor import run_doctor
from recall_engine.drive import (
    DriveError,
    build_drive_service,
    resolve_folder_id,
    sync_download,
    sync_upload,
)
from recall_engine.launcher import (
    PI_MCP_ADAPTER,
    LauncherError,
    detect_agent,
    launch_agent,
    pi_mcp_adapter_installed,
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
    log_path,
    release_server,
)
from recall_engine.repo import RepoError, ensure_repo
from recall_engine.skill import (
    SkillError,
    detect_active_repo,
    inject_skill,
    restore_skill,
)

LOCAL_KNOWLEDGE_PATH_HELP = (
    "Path to an existing local knowledge repo; its notes live under <path>/src/. "
    "Optional for a wrap in a directory that already has a running session, where "
    "the repo is auto-detected."
)
REMOTE_KNOWLEDGE_FOLDER_HELP = (
    "Google Drive folder ID or name (case-sensitive); enables `sync download` / "
    "`sync upload` and the first-wrap auto-download."
)

app = typer.Typer(
    no_args_is_help=True,
    help="""Recall Engine: wrap an agent CLI (claude/codex/pi/gemini/opencode/agy) with a knowledge repo and sync it with Google Drive.

--local-knowledge-path and --remote-knowledge-folder also work on `wrap`, `sync` and `doctor`, where they override the values given here. With `wrap`, pass them before AGENT, since everything after AGENT goes to the agent CLI.

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
    local_knowledge_path: str = typer.Option(
        None, "--local-knowledge-path", help=LOCAL_KNOWLEDGE_PATH_HELP
    ),
    remote_knowledge_folder: str = typer.Option(
        None, "--remote-knowledge-folder", help=REMOTE_KNOWLEDGE_FOLDER_HELP
    ),
    uninstall_completion_flag: bool = typer.Option(
        None,
        "--uninstall-completion",
        callback=_uninstall_completion_callback,
        expose_value=False,
        help="Uninstall completion for the current shell (bash/zsh/fish; "
        "PowerShell profiles are left alone).",
    ),
) -> None:
    ctx.obj = {
        "local_knowledge_path": local_knowledge_path,
        "remote_knowledge_folder": remote_knowledge_folder,
    }


def _resolve_options(
    ctx: typer.Context, local_knowledge_path: str, remote_knowledge_folder: str
) -> tuple[str, str]:
    """Command-level options win; fall back to the ones passed before the command."""
    global_options = ctx.obj or {}
    return (
        local_knowledge_path or global_options.get("local_knowledge_path"),
        remote_knowledge_folder or global_options.get("remote_knowledge_folder"),
    )


def _spawn_background_download(repo: Path, drive_folder: str) -> None:
    """First wrap of a repo: fire-and-forget `sync download` in the background.

    Never waited on, so a slow or failing sync cannot disrupt wrap or the agent;
    stderr goes to the shared server log instead of interleaving with the TUI.
    """
    try:
        log = open(log_path(), "ab")  # noqa: SIM115 (kept open for the child)
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "recall_engine",
                "sync",
                "download",
                "--local-knowledge-path",
                str(repo),
                "--remote-knowledge-folder",
                drive_folder,
            ],
            stdout=subprocess.DEVNULL,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # best-effort: a spawn failure must not disrupt wrap


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
    local_knowledge_path: str = typer.Option(
        None, "--local-knowledge-path", help=LOCAL_KNOWLEDGE_PATH_HELP
    ),
    remote_knowledge_folder: str = typer.Option(
        None, "--remote-knowledge-folder", help=REMOTE_KNOWLEDGE_FOLDER_HELP
    ),
) -> None:
    """Prepare the knowledge repo and launch AGENT (claude/codex/pi/gemini/opencode/agy or a wrapper).

    Extra arguments after AGENT are passed straight through to it, so pass these
    options before AGENT.
    """
    agent_args = list(ctx.args)
    local_knowledge_path, remote_knowledge_folder = _resolve_options(
        ctx, local_knowledge_path, remote_knowledge_folder
    )
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
    # pi only reaches the MCP server through the pi-mcp-adapter extension; block
    # launch until it is installed (skill-only pi would silently miss the tools).
    # `is False` only: None means pi is not runnable, left to launch_agent's
    # 'install pi' error below.
    if family == "pi" and pi_mcp_adapter_installed(agent) is False:
        typer.echo(
            f"pi is missing the {PI_MCP_ADAPTER} extension, which it needs to "
            "reach the recall-engine MCP server. Install it and re-run:\n"
            f"    pi install npm:{PI_MCP_ADAPTER}",
            err=True,
        )
        raise typer.Exit(2)
    try:
        # A live wrap session in this dir lets a second wrap reuse its repo
        # without --local-knowledge-path; the option still wins when passed.
        settings = resolve_settings(
            local_knowledge_path=local_knowledge_path,
            remote_knowledge_folder=remote_knowledge_folder,
            fallback_repo_path=detect_active_repo(),
        )
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        repo = ensure_repo(settings)
    except RepoError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"knowledge repo: {repo}")
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
        inject_skill(repo)
        server = ensure_server(repo)
        inject_mcp_config(family, repo, server.url, server.token)
        # First wrap to bring this repo online pulls the latest notes from Drive.
        if server.repo_first:
            if settings.drive_folder:
                typer.echo(
                    f"drive sync: downloading '{settings.drive_folder}' into "
                    f"{repo / 'src'} in the background"
                )
                _spawn_background_download(repo, settings.drive_folder)
            else:
                typer.echo(
                    "drive sync: skipped (pass --remote-knowledge-folder to "
                    "auto-download notes on first wrap)"
                )
        typer.echo(f"launching {agent}...")
        # agy only reads the injected .agents/ config when the project dir is in
        # its workspace, so pass `--add-dir <project>` for it.
        pre_args = (
            ["--add-dir", str(Path.cwd())]
            if AGENTS[family].needs_workspace_dir
            else []
        )
        exit_code = launch_agent(
            repo,
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
def sync(
    ctx: typer.Context,
    mode: str,
    local_knowledge_path: str = typer.Option(
        None, "--local-knowledge-path", help=LOCAL_KNOWLEDGE_PATH_HELP
    ),
    remote_knowledge_folder: str = typer.Option(
        None, "--remote-knowledge-folder", help=REMOTE_KNOWLEDGE_FOLDER_HELP
    ),
) -> None:
    """One-way Drive sync; MODE is 'download' or 'upload'."""
    if mode not in ("download", "upload"):
        typer.echo(f"Invalid mode '{mode}'; use 'download' or 'upload'.", err=True)
        raise typer.Exit(2)
    local_knowledge_path, remote_knowledge_folder = _resolve_options(
        ctx, local_knowledge_path, remote_knowledge_folder
    )
    try:
        settings = resolve_settings(
            local_knowledge_path=local_knowledge_path,
            remote_knowledge_folder=remote_knowledge_folder,
        )
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if not settings.drive_folder:
        typer.echo(
            "No Drive folder configured; pass --remote-knowledge-folder with the "
            "Drive folder ID or name.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        repo = ensure_repo(settings)
    except RepoError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    src_dir = repo / "src"
    try:
        typer.echo("drive sync: authenticating")
        service = build_drive_service()
        typer.echo(f"drive sync: resolving folder {settings.drive_folder}")
        folder_id = resolve_folder_id(service, settings.drive_folder)
        typer.echo(f"drive sync: starting {mode}")
        if mode == "download":
            names = sync_download(service, folder_id, src_dir, log=typer.echo)
        else:
            names = sync_upload(service, folder_id, src_dir, log=typer.echo)
    except DriveError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for name in names:
        typer.echo(name)
    typer.echo(f"synced {len(names)} file(s) ({mode})")


@app.command()
def doctor(
    ctx: typer.Context,
    local_knowledge_path: str = typer.Option(
        None, "--local-knowledge-path", help=LOCAL_KNOWLEDGE_PATH_HELP
    ),
    remote_knowledge_folder: str = typer.Option(
        None, "--remote-knowledge-folder", help=REMOTE_KNOWLEDGE_FOLDER_HELP
    ),
) -> None:
    """Diagnose repo config, gcloud Drive access, and agent CLIs on PATH."""
    local_knowledge_path, remote_knowledge_folder = _resolve_options(
        ctx, local_knowledge_path, remote_knowledge_folder
    )
    if not run_doctor(local_knowledge_path, remote_knowledge_folder):
        raise typer.Exit(1)


@app.command(name="mcp-serve", hidden=True)
def mcp_serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
) -> None:
    """Run the shared knowledge MCP server (spawned by `wrap`; internal use)."""
    run_server(host, port, token or None)
