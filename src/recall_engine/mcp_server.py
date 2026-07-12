"""Machine-wide MCP server exposing knowledge repos over streamable HTTP.

A single server serves many knowledge repos. Each client connection names its
target repo in the ``X-Recall-Repo`` request header (an absolute path), and may
carry an auth token in ``X-Recall-Token``. Notes live under ``<repo>/src/**/*.md``.
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import Context, FastMCP

from recall_engine import search
from recall_engine.template_renderer import render_template

INSTRUCTIONS = render_template("rules/mcp_server_instructions.md").strip()

MAX_MATCHES = 50
NOTE_RESOURCE_TEMPLATE = "recall://note/{encoded_path}"
NOTES_INDEX_RESOURCE = "recall://notes/index"
ENCODED_NOTE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def encode_note_resource_path(relative_path: str) -> str:
    """Encode a repo-relative note path for a single URI template segment."""
    return base64.urlsafe_b64encode(relative_path.encode()).decode().rstrip("=")


def decode_note_resource_path(encoded_path: str) -> str:
    if not ENCODED_NOTE_PATH_PATTERN.fullmatch(encoded_path):
        raise ValueError(f"invalid encoded note path: {encoded_path}")
    padding = "=" * (-len(encoded_path) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{encoded_path}{padding}".encode())
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid encoded note path: {encoded_path}") from exc
    try:
        relative_path = decoded.decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid encoded note path: {encoded_path}") from exc
    if not relative_path or "\x00" in relative_path:
        raise ValueError(f"invalid encoded note path: {encoded_path}")
    if encode_note_resource_path(relative_path) != encoded_path:
        raise ValueError(f"invalid encoded note path: {encoded_path}")
    return relative_path


def create_server(token: str | None = None) -> FastMCP:
    """Build the recall-engine server with its search tool and note resources.

    If ``token`` is not None, every tool and resource request rejects a client
    whose ``X-Recall-Token`` header does not equal ``token``.
    """
    mcp = FastMCP("recall-engine", stateless_http=True, instructions=INSTRUCTIONS)

    def resolve_repo(ctx: Context) -> Path:
        """Resolve and authorize the target repo from the request headers."""
        headers = ctx.request_context.request.headers
        repo_header = headers.get("x-recall-repo")
        if not repo_header:
            raise ValueError("missing X-Recall-Repo header")
        repo = Path(repo_header).resolve()
        if not repo.is_dir() or not (repo / "src").is_dir():
            raise ValueError(
                f"X-Recall-Repo does not point to a knowledge repo with a "
                f"src/ directory: {repo_header}"
            )
        if token is not None and headers.get("x-recall-token") != token:
            raise ValueError("invalid or missing X-Recall-Token")
        return repo

    def resolve_note(src: Path, path: str) -> Path:
        """Resolve a note path and keep the request inside <repo>/src.

        Lexical, not realpath-based: a symlink under src/ is a note even when it
        points outside src/, so only '..' escapes and paths outside src/ are
        rejected — see search.is_note_inside. '..' is rejected outright because
        a symlinked dir makes the lexical parent differ from the real one.
        """
        candidate = Path(path)
        note = candidate if candidate.is_absolute() else src / candidate
        if ".." in note.parts or not search.is_note_inside(src, note):
            raise ValueError(f"path is outside the knowledge repo src/ directory: {path}")
        if not note.is_file():
            raise ValueError(f"note not found: {path}")
        return note

    def note_resource_uri(src: Path, note: Path) -> str:
        relative_path = note.relative_to(src).as_posix()
        return f"recall://note/{encode_note_resource_path(relative_path)}"

    def note_index_entry(src: Path, note: Path) -> dict[str, str]:
        return {
            "path": str(note),
            "relative_path": note.relative_to(src).as_posix(),
            "resource_uri": note_resource_uri(src, note),
        }

    @mcp.tool()
    def search_knowledge(query: str, ctx: Context) -> list[dict]:
        """Case-insensitive substring search across <repo>/src/**/*.md.

        Runs ugrep when it is on PATH; falls back to a built-in scan otherwise.
        """
        src = (resolve_repo(ctx) / "src").resolve()
        return [
            {
                "path": str(note),
                "line": lineno,
                "snippet": line.strip(),
                "resource_uri": note_resource_uri(src, note),
            }
            for note, lineno, line in search.search_notes(src, query, MAX_MATCHES)
        ]

    # Add list/read tool adapters only if a supported client cannot use MCP resources.
    @mcp.resource(
        NOTES_INDEX_RESOURCE,
        name="notes_index",
        title="Recall Engine Notes Index",
        description=render_template("resources/notes_index_description.md.j2").strip(),
        mime_type="application/json",
    )
    def notes_index() -> list[dict[str, str]]:
        """List Markdown notes in the selected recall-engine knowledge repo."""
        ctx = mcp.get_context()
        src = (resolve_repo(ctx) / "src").resolve()
        return [
            note_index_entry(src, note)
            for note in search.iter_note_paths(src)
        ]

    @mcp.resource(
        NOTE_RESOURCE_TEMPLATE,
        name="note",
        title="Recall Engine Note",
        description="Read one Markdown note from the selected recall-engine knowledge repo.",
        mime_type="text/markdown",
    )
    def note(encoded_path: str, ctx: Context) -> str:
        """Read one Markdown note from the selected recall-engine knowledge repo."""
        src = (resolve_repo(ctx) / "src").resolve()
        note_path = decode_note_resource_path(encoded_path)
        return resolve_note(src, note_path).read_text(encoding="utf-8", errors="replace")

    return mcp


def run_server(host: str, port: int, token: str | None = None) -> None:
    """Serve the recall-engine MCP server over streamable HTTP (blocking)."""
    mcp = create_server(token)
    app = mcp.streamable_http_app()
    uvicorn.run(app, host=host, port=port, log_level="error")
