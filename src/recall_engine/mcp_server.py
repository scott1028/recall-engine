"""Machine-wide MCP server exposing knowledge paths over streamable HTTP.

A single server serves many wrap sessions. Each client connection names its
knowledge roots in the ``X-Recall-Paths`` request header (a JSON array of
absolute directory paths), and may carry an auth token in ``X-Recall-Token``.
Notes live under ``<root>/**/*.md`` for each root.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
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
# the SDK default (ttlMs=0, private): notes change on disk and depend on the
# caller's knowledge paths.
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


def encode_note_resource_path(absolute_path: str) -> str:
    """Encode an absolute note path for a single URI template segment."""
    return base64.urlsafe_b64encode(absolute_path.encode()).decode().rstrip("=")


def decode_note_resource_path(encoded_path: str) -> str:
    if not ENCODED_NOTE_PATH_PATTERN.fullmatch(encoded_path):
        raise ValueError(f"invalid encoded note path: {encoded_path}")
    padding = "=" * (-len(encoded_path) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{encoded_path}{padding}".encode())
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid encoded note path: {encoded_path}") from exc
    try:
        absolute_path = decoded.decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid encoded note path: {encoded_path}") from exc
    if not absolute_path or "\x00" in absolute_path:
        raise ValueError(f"invalid encoded note path: {encoded_path}")
    if encode_note_resource_path(absolute_path) != encoded_path:
        raise ValueError(f"invalid encoded note path: {encoded_path}")
    return absolute_path


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

    def resolve_roots() -> list[Path]:
        """Resolve and authorize the knowledge roots from the request headers.

        Raises ResourceError: the SDK passes its message on from resources and,
        as an is_error result, from tools.
        """
        headers = _request_headers.get() or {}
        paths_header = headers.get("x-recall-paths")
        if not paths_header:
            raise ResourceError("missing X-Recall-Paths header")
        try:
            raw_paths = json.loads(paths_header)
        except ValueError:
            raw_paths = None
        if not isinstance(raw_paths, list) or not raw_paths or not all(
            isinstance(raw, str) and raw for raw in raw_paths
        ):
            raise ResourceError(
                f"X-Recall-Paths must be a non-empty JSON array of directory "
                f"paths: {paths_header}"
            )
        roots = []
        for raw in raw_paths:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                raise ResourceError(
                    f"X-Recall-Paths entry is not an existing directory: {raw}"
                )
            roots.append(root)
        if token is not None and headers.get("x-recall-token") != token:
            raise ResourceError("invalid or missing X-Recall-Token")
        return roots

    def resolve_note(roots: Sequence[Path], path: str) -> Path:
        """Resolve an absolute note path and keep the request inside a root.

        Lexical, not realpath-based: a symlink under a root/ is a note even when
        it points outside the root/, so only '..' escapes and paths outside every
        root are rejected — see search.is_note_inside. '..' is rejected outright
        because a symlinked dir makes the lexical parent differ from the real one.
        """
        candidate = Path(path)
        if ".." in candidate.parts or not search.is_note_inside_roots(roots, candidate):
            raise ResourceError(f"path is outside the knowledge paths: {path}")
        if not candidate.is_file():
            raise ResourceNotFoundError(f"note not found: {path}")
        return candidate

    def note_resource_uri(note: Path) -> str:
        # Encode the absolute path so notes with the same path under different
        # roots cannot collide.
        return f"recall://note/{encode_note_resource_path(note.as_posix())}"

    def note_relative_path(roots: Sequence[Path], note: Path) -> str:
        """Path of the note relative to its root; the deepest root wins when
        roots nest."""
        containing = [root for root in roots if search.is_note_inside(root, note)]
        if not containing:
            return note.as_posix()
        root = max(containing, key=lambda root: len(str(root)))
        return note.relative_to(root).as_posix()

    def note_index_entry(roots: Sequence[Path], note: Path) -> dict[str, str]:
        return {
            "path": str(note),
            "relative_path": note_relative_path(roots, note),
            "resource_uri": note_resource_uri(note),
        }

    @mcp.tool()
    def search_knowledge(query: str) -> list[dict]:
        """Case-insensitive substring search across <path>/**/*.md for every
        configured knowledge path.

        Runs ugrep when it is on PATH; falls back to a built-in scan otherwise.
        """
        roots = resolve_roots()
        return [
            {
                "path": str(note),
                "line": lineno,
                "snippet": line.strip(),
                "resource_uri": note_resource_uri(note),
            }
            for note, lineno, line in search.search_notes(roots, query, MAX_MATCHES)
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
        """List Markdown notes in the selected recall-engine knowledge paths."""
        roots = resolve_roots()
        return [note_index_entry(roots, note) for note in search.iter_note_paths(roots)]

    @mcp.resource(
        NOTE_RESOURCE_TEMPLATE,
        name="note",
        title="Recall Engine Note",
        description="Read one Markdown note from the selected recall-engine knowledge paths.",
        mime_type="text/markdown",
    )
    def note(encoded_path: str) -> str:
        """Read one Markdown note from the selected recall-engine knowledge paths."""
        roots = resolve_roots()
        try:
            note_path = decode_note_resource_path(encoded_path)
        except ValueError as exc:
            raise ResourceError(str(exc)) from exc
        return resolve_note(roots, note_path).read_text(encoding="utf-8", errors="replace")

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
