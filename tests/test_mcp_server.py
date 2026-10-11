"""End-to-end tests for the recall-engine MCP server.
Each test starts a real streamable-HTTP server in a background thread and drives
it through a real mcp Client, once per protocol era: the initialize handshake
(what current agent CLIs speak) and the 2026-07-28 per-request protocol. The
project uses plain pytest (no pytest-asyncio), so async flows run via
asyncio.run inside sync tests.
"""
import asyncio
import base64
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from recall_engine import __version__
from recall_engine.mcp_server import create_app

# "legacy" forces the initialize handshake; "auto" probes server/discover and
# adopts the 2026-07-28 protocol.
CLIENT_MODES = ["legacy", "auto"]
MODERN_PROTOCOL_VERSION = "2026-07-28"
@dataclass(frozen=True)
class Endpoint:
    """A running test server plus the protocol mode the client speaks to it."""
    url: str
    mode: str
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
def _start_server(token: str | None = None) -> str:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(token), host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True).start()
    time.sleep(1.5)
    return f"http://127.0.0.1:{port}/mcp"
async def _run_client(endpoint, request, headers):
    async with httpx2.AsyncClient(headers=headers) as http_client:
        transport = streamable_http_client(endpoint.url, http_client=http_client)
        async with Client(transport, mode=endpoint.mode) as client:
            return await request(client)
def run_client(endpoint, request, *, repo=None, token=None):
    """Open a fresh client connection, run one request, return its result."""
    headers = {}
    if repo is not None:
        headers["X-Recall-Repo"] = repo
    if token is not None:
        headers["X-Recall-Token"] = token
    return asyncio.run(_run_client(endpoint, request, headers))
def call_tool(endpoint, tool, args, *, repo=None, token=None):
    return run_client(
        endpoint, lambda client: client.call_tool(tool, args), repo=repo, token=token
    )
def read_resource(endpoint, uri, *, repo=None, token=None):
    return run_client(
        endpoint, lambda client: client.read_resource(uri), repo=repo, token=token
    )
def exception_text(exc: BaseException) -> str:
    messages = [str(exc)]
    nested_exceptions = getattr(exc, "exceptions", None)
    if nested_exceptions:
        for nested in nested_exceptions:
            messages.append(exception_text(nested))
    return "\n".join(messages)
def make_repo(base: Path, notes: dict[str, str]) -> Path:
    """Create <base>/src with the given {relative_md_path: text} notes."""
    src = base / "src"
    src.mkdir(parents=True)
    for rel, text in notes.items():
        note = src / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(text)
    return base
def note_path(repo: Path, rel: str) -> str:
    return str((repo / "src" / rel).resolve())
def note_resource_uri(rel: str) -> str:
    encoded = base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")
    return f"recall://note/{encoded}"
@pytest.fixture(scope="module")
def server_url():
    return _start_server()
@pytest.fixture(scope="module")
def token_server_url():
    return _start_server(token="s3cr3t")
@pytest.fixture(params=CLIENT_MODES)
def server(request, server_url):
    return Endpoint(server_url, request.param)
@pytest.fixture(params=CLIENT_MODES)
def token_server(request, token_server_url):
    return Endpoint(token_server_url, request.param)
def test_serves_handshake_and_stateless_protocol_eras(server):
    async def snapshot(client):
        return client.protocol_version, client.server_info, await client.list_tools()
    protocol_version, server_info, tools = run_client(server, snapshot)
    assert (server_info.name, server_info.version) == ("recall-engine", __version__)
    if server.mode == "legacy":
        assert protocol_version < MODERN_PROTOCOL_VERSION
    else:
        assert protocol_version == MODERN_PROTOCOL_VERSION
        # The 2026-07-28 wire carries cache hints: tool lists are static.
        assert (tools.ttl_ms, tools.cache_scope) == (3_600_000, "public")
def test_public_mcp_shape_is_advertised(server):
    tools = run_client(server, lambda client: client.list_tools())
    assert [tool.name for tool in tools.tools] == ["search_knowledge"]
    resources_result = run_client(server, lambda client: client.list_resources())
    resources = {str(resource.uri): resource for resource in resources_result.resources}
    assert "recall://notes/index" in resources
    assert resources["recall://notes/index"].mime_type == "application/json"
    assert (
        "Before calling `search_knowledge` or reading `recall://notes/index`, "
        "predict useful expansion keywords"
    ) in resources["recall://notes/index"].description
    templates_result = run_client(server, lambda client: client.list_resource_templates())
    templates = {
        template.uri_template: template for template in templates_result.resource_templates
    }
    assert "recall://note/{encoded_path}" in templates
    assert templates["recall://note/{encoded_path}"].mime_type == "text/markdown"
def test_search_knowledge_hit_and_miss(server, tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {
            "guide.md": "Deploy uses blue-green rollout.\nRollback is manual.\n",
            "sub/notes.md": "Nothing relevant here.\n",
        },
    )
    hit = call_tool(server, "search_knowledge", {"query": "blue-green"}, repo=str(repo))
    assert not hit.is_error
    matches = hit.structured_content["result"]
    assert len(matches) == 1
    assert matches[0]["path"] == note_path(repo, "guide.md")
    assert matches[0]["line"] == 1
    assert "blue-green" in matches[0]["snippet"]
    assert matches[0]["resource_uri"] == note_resource_uri("guide.md")
    miss = call_tool(server, "search_knowledge", {"query": "kubernetes"}, repo=str(repo))
    assert not miss.is_error
    assert miss.structured_content["result"] == []
def test_search_falls_back_to_scan_without_ugrep(server, tmp_path, monkeypatch):
    # ugrep is optional: with it off PATH the tool still answers, via the scan.
    repo = make_repo(tmp_path / "fb", {"n.md": "fallback keyword here\n"})
    monkeypatch.setattr("recall_engine.search.ugrep_path", lambda: None)
    res = call_tool(server, "search_knowledge", {"query": "keyword"}, repo=str(repo))
    assert not res.is_error
    assert [m["path"] for m in res.structured_content["result"]] == [note_path(repo, "n.md")]
def test_header_routing_between_repos(server, tmp_path):
    repo_a = make_repo(tmp_path / "a", {"a.md": "alpha keyword here\n"})
    repo_b = make_repo(tmp_path / "b", {"b.md": "alpha keyword here\n"})
    res_a = call_tool(server, "search_knowledge", {"query": "alpha"}, repo=str(repo_a))
    res_b = call_tool(server, "search_knowledge", {"query": "alpha"}, repo=str(repo_b))
    assert [m["path"] for m in res_a.structured_content["result"]] == [note_path(repo_a, "a.md")]
    assert [m["path"] for m in res_b.structured_content["result"]] == [note_path(repo_b, "b.md")]
def test_notes_index_resource_uses_request_repo(server, tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {"b.md": "x\n", "a.md": "y\n", "sub/c.md": "z\n"},
    )
    res = read_resource(server, "recall://notes/index", repo=str(repo))
    content = res.contents[0]
    assert content.mime_type == "application/json"
    assert json.loads(content.text) == [
        {
            "path": note_path(repo, "a.md"),
            "relative_path": "a.md",
            "resource_uri": note_resource_uri("a.md"),
        },
        {
            "path": note_path(repo, "b.md"),
            "relative_path": "b.md",
            "resource_uri": note_resource_uri("b.md"),
        },
        {
            "path": note_path(repo, "sub/c.md"),
            "relative_path": "sub/c.md",
            "resource_uri": note_resource_uri("sub/c.md"),
        },
    ]
def test_note_resource_reads_nested_markdown_note(server, tmp_path):
    repo = make_repo(tmp_path / "repo", {"sub/doc.md": "# Title\nfull note text\n"})
    res = read_resource(server, note_resource_uri("sub/doc.md"), repo=str(repo))
    content = res.contents[0]
    assert content.mime_type == "text/markdown"
    assert str(content.uri) == note_resource_uri("sub/doc.md")
    assert content.text == "# Title\nfull note text\n"
    # Reads are never cached: notes change on disk and depend on the repo.
    assert (res.ttl_ms, res.cache_scope) == (0, "private")
def test_note_resource_rejects_invalid_paths(server, tmp_path):
    repo = make_repo(tmp_path / "repo", {"doc.md": "safe note\n"})
    (repo / "secret.md").write_text("outside src\n")
    cases = [
        ("recall://note/not-valid-***", "invalid encoded note path"),
        (f"{note_resource_uri('doc.md')}***", "invalid encoded note path"),
        (note_resource_uri("../secret.md"), "outside"),
        # Any '..' is rejected: through a symlinked dir the lexical parent is
        # not the real one, so the containment check cannot see where it lands.
        (note_resource_uri("linkdir/../secret.md"), "outside"),
    ]
    for uri, expected_message in cases:
        with pytest.raises(Exception) as exc_info:
            read_resource(server, uri, repo=str(repo))
        assert expected_message in exception_text(exc_info.value)
def test_requests_require_repo_header_for_tool_and_resource(server):
    res = call_tool(server, "search_knowledge", {"query": "keyword"})
    assert res.is_error
    assert "X-Recall-Repo" in res.content[0].text
    with pytest.raises(Exception) as exc_info:
        read_resource(server, "recall://notes/index")
    assert "X-Recall-Repo" in exception_text(exc_info.value)
def test_token_auth_for_tool_and_resource(token_server, tmp_path):
    repo = make_repo(tmp_path / "repo", {"n.md": "token protected keyword\n"})
    no_token = call_tool(
        token_server,
        "search_knowledge",
        {"query": "keyword"},
        repo=str(repo),
    )
    assert no_token.is_error
    assert "X-Recall-Token" in no_token.content[0].text
    wrong_token = call_tool(
        token_server,
        "search_knowledge",
        {"query": "keyword"},
        repo=str(repo),
        token="nope",
    )
    assert wrong_token.is_error
    good = call_tool(
        token_server,
        "search_knowledge",
        {"query": "keyword"},
        repo=str(repo),
        token="s3cr3t",
    )
    assert not good.is_error
    assert [match["path"] for match in good.structured_content["result"]] == [
        note_path(repo, "n.md")
    ]
    with pytest.raises(Exception) as exc_info:
        read_resource(token_server, "recall://notes/index", repo=str(repo))
    assert "X-Recall-Token" in exception_text(exc_info.value)
    with pytest.raises(Exception) as exc_info:
        read_resource(
            token_server,
            note_resource_uri("n.md"),
            repo=str(repo),
            token="nope",
        )
    assert "X-Recall-Token" in exception_text(exc_info.value)
    index = read_resource(
        token_server,
        "recall://notes/index",
        repo=str(repo),
        token="s3cr3t",
    )
    assert json.loads(index.contents[0].text) == [
        {
            "path": note_path(repo, "n.md"),
            "relative_path": "n.md",
            "resource_uri": note_resource_uri("n.md"),
        }
    ]
    note = read_resource(
        token_server,
        note_resource_uri("n.md"),
        repo=str(repo),
        token="s3cr3t",
    )
    assert note.contents[0].text == "token protected keyword\n"
def test_note_symlinked_outside_src_is_searched_indexed_and_read(server, tmp_path):
    # A symlink under src/ is a note its owner mounted: search, index and read
    # all serve it. note_path() cannot be used for it — it resolves symlinks,
    # while the server reports the path the note was reached by.
    repo = make_repo(tmp_path / "repo", {"safe.md": "inside keyword\n"})
    outside = tmp_path / "outside.md"
    outside.write_text("outside keyword\n")
    os.symlink(outside, repo / "src" / "external.md")
    external_path = str((repo / "src").resolve() / "external.md")
    search = call_tool(
        server,
        "search_knowledge",
        {"query": "outside"},
        repo=str(repo),
    )
    assert not search.is_error
    assert [match["path"] for match in search.structured_content["result"]] == [
        external_path
    ]
    index = read_resource(server, "recall://notes/index", repo=str(repo))
    assert json.loads(index.contents[0].text) == [
        {
            "path": external_path,
            "relative_path": "external.md",
            "resource_uri": note_resource_uri("external.md"),
        },
        {
            "path": note_path(repo, "safe.md"),
            "relative_path": "safe.md",
            "resource_uri": note_resource_uri("safe.md"),
        },
    ]
    note = read_resource(
        server, note_resource_uri("external.md"), repo=str(repo)
    )
    assert note.contents[0].text == "outside keyword\n"
