"""Machine-wide MCP server exposing knowledge repos over streamable HTTP.

A single server serves many knowledge repos. Each client connection names its
target repo in the ``X-Recall-Repo`` request header (an absolute path), and may
carry an auth token in ``X-Recall-Token``. Notes live under ``<repo>/src/**/*.md``.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from mcp.server.caching import CacheHint
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from recall_engine import __version__, search
from recall_engine.template_renderer import render_template

if TYPE_CHECKING:
    from starlette.applications import Starlette

INSTRUCTIONS = render_template("rules/mcp_server_instructions.md").strip()

MAX_MATCHES = 50
NOTE_RESOURCE_TEMPLATE = "recall://note/{encoded_path}"
NOTES_INDEX_RESOURCE = "recall://notes/index"
ENCODED_NOTE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Lists never vary per request, so clients may cache and share them. Reads keep
# the SDK default (ttlMs=0, private): notes change on disk and depend on the repo.
LIST_CACHE_HINT = CacheHint(ttl_ms=3_600_000, scope="public")
CACHE_HINTS = {
    "tools/list": LIST_CACHE_HINT,
    "resources/list": LIST_CACHE_HINT,
    "resources/templates/list": LIST_CACHE_HINT,
}

# Headers of the request being handled. The SDK injects no Context into a static
# resource (recall://notes/index), so a middleware publishes them here instead.
_request_headers: ContextVar[Mapping[str, str] | None] = ContextVar(
    "recall_request_headers", default=None
)


async def _publish_request_headers(
    ctx: ServerRequestContext[Any, Any], call_next: CallNext
) -> HandlerResult:
    token = _request_headers.set(getattr(ctx.request, "headers", None))
    try:
        return await call_next(ctx)
    finally:
        _request_headers.reset(token)


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


def create_server(token: str | None = None) -> MCPServer:
    """Build the recall-engine server with its search tool and note resources.

    If ``token`` is not None, every tool and resource request rejects a client
    whose ``X-Recall-Token`` header does not equal ``token``.
    """
    mcp = MCPServer(
        "recall-engine",
        instructions=INSTRUCTIONS,
        version=__version__,
        cache_hints=CACHE_HINTS,
        middleware=[_publish_request_headers],
    )

    def resolve_repo() -> Path:
        """Resolve and authorize the target repo from the request headers.

        Raises ResourceError: the SDK passes its message on from resources and,
        as an is_error result, from tools.
        """
        headers = _request_headers.get() or {}
        repo_header = headers.get("x-recall-repo")
        if not repo_header:
            raise ResourceError("missing X-Recall-Repo header")
        repo = Path(repo_header).resolve()
        if not repo.is_dir() or not (repo / "src").is_dir():
            raise ResourceError(
                f"X-Recall-Repo does not point to a knowledge repo with a "
                f"src/ directory: {repo_header}"
            )
        if token is not None and headers.get("x-recall-token") != token:
            raise ResourceError("invalid or missing X-Recall-Token")
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
            raise ResourceError(f"path is outside the knowledge repo src/ directory: {path}")
        if not note.is_file():
            raise ResourceNotFoundError(f"note not found: {path}")
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
    def search_knowledge(query: str) -> list[dict]:
        """Case-insensitive substring search across <repo>/src/**/*.md.

        Runs ugrep when it is on PATH; falls back to a built-in scan otherwise.
        """
        src = (resolve_repo() / "src").resolve()
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
        src = (resolve_repo() / "src").resolve()
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
    def note(encoded_path: str) -> str:
        """Read one Markdown note from the selected recall-engine knowledge repo."""
        src = (resolve_repo() / "src").resolve()
        try:
            note_path = decode_note_resource_path(encoded_path)
        except ValueError as exc:
            raise ResourceError(str(exc)) from exc
        return resolve_note(src, note_path).read_text(encoding="utf-8", errors="replace")

    return mcp


def create_app(token: str | None = None, host: str = "127.0.0.1") -> Starlette:
    """Build the stateless streamable-HTTP ASGI app.

    Stateless serves both handshake-era clients and 2026-07-28 per-request
    clients without keeping sessions.
    """
    return create_server(token).streamable_http_app(stateless_http=True, host=host)


def run_server(host: str, port: int, token: str | None = None) -> None:
    """Serve the recall-engine MCP server over streamable HTTP (blocking)."""
    uvicorn.run(create_app(token, host), host=host, port=port, log_level="error")
