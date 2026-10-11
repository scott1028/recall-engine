# recall-engine

`recall-engine` is a thin launcher that wraps an agent CLI (`claude`,
`codex`, `pi`, `gemini`, `opencode`, or `agy`) with one or more existing local
Markdown knowledge directories. It resolves the knowledge paths, starts
(or reuses) a shared MCP server that serves their notes, injects a skill
that makes the agent consult those notes — its record of past processing and
notes — before replying to any message, points the agent's MCP config at the
server, launches the agent, and restores everything on exit.

The knowledge base is reached through an MCP tool and resources rather than the
filesystem, so the knowledge directories can live outside the project without a
sandboxed agent needing access to them.

## Install

From a checkout of this repository:

```bash
uv tool install .
# or
pipx install .
```

Requirements: Python >= 3.10, and at least one supported agent CLI:
Claude Code (https://claude.com/claude-code), Codex CLI
(https://developers.openai.com/codex/cli), pi
(https://github.com/earendil-works/pi), Gemini CLI
(https://github.com/google-gemini/gemini-cli), opencode, or agy. The install
provides the `recall-engine` entry point.

## Quickstart

```bash
# one or more local knowledge directories (repeat --knowledge-path)
recall-engine wrap \
  --knowledge-path ~/workspace/notes \
  --knowledge-path ~/workspace/architecture-docs \
  claude

# other agent CLIs
recall-engine wrap --knowledge-path ~/workspace/notes codex
recall-engine wrap --knowledge-path ~/workspace/notes pi
recall-engine wrap --knowledge-path ~/workspace/notes gemini
recall-engine wrap --knowledge-path ~/workspace/notes opencode
recall-engine wrap --knowledge-path ~/workspace/notes agy
```

## How wrap works

`recall-engine wrap [options] <agent> [args...]` runs a six-step lifecycle.
Anything after `<agent>` is forwarded verbatim to the agent — so pass
recall-engine's own options **before** `<agent>` — and leading environment
variables are inherited, so
`AA=1 recall-engine wrap claude foo --bar` behaves like
`AA=1 claude foo --bar`:

1. **Resolve the knowledge paths** — each `--knowledge-path` selects an
   existing local knowledge directory (its notes are found under
   `<path>/**/*.md`), and the command fails if a directory is missing.
2. **Inject the skill** — a rendered `SKILL.md` is written to
   `.agents/skills/recall-engine/` (the Agent Skills SSOT) in the
   current project, and `.claude/skills/`, `.gemini/skills/`, `.pi/skills/`,
   and `.opencode/skills/` get relative symlinks pointing at it, so every
   supported agent discovers the same skill (`codex` and `agy` read the SSOT
   directly, so they need no symlink). Any pre-existing skill entry is backed
   up first, and a marker file tracks the session owners: multiple wrap
   sessions in the same directory attach to one injection (the marker records
   every owner pid), and the last session to exit restores the original state.
3. **Start or reuse the MCP server** — one server per machine serves every wrap
   session (see [Shared MCP server](#shared-mcp-server)).
4. **Inject the MCP config** — the server is registered in the agent's own
   config file (`.mcp.json` for claude, `.codex/config.toml` for codex,
   `.pi/mcp.json` for pi, `.gemini/settings.json` for gemini, `opencode.json`
   for opencode, `.agents/mcp_config.json` for agy). Only the `recall-engine`
   entry is added; existing content is backed up and restored on exit.
   opencode gets the v2 shape (`mcp.servers.<name>`), which opencode v1 cannot
   read.
5. **Launch the agent** — as a child process with signals forwarded and
   `RECALL_REPO_PATH` set to the first resolved knowledge path. Unknown command
   names (e.g. `claude-company`) are classified via their `--version` output
   or name tokens before launching.
6. **You work, then restore on exit** — before replying to any message, the
   skill directs the agent to call `search_knowledge`, read matching notes via
   the returned resource URI, and cite the note paths it returns, factoring
   prior processing records into the answer as past experience. Normal exit or
   Ctrl+C removes the injected skill, symlinks, and MCP config entry, restores
   any backups, drops this session's claim on the shared server, and returns the
   agent's exit code.

You can run several wrap sessions concurrently in the same project directory:
the first sets up the injected skill and each later one attaches to it, so the
skill stays in place until the last session exits. A later session may omit
`--knowledge-path` — when it is not passed, the knowledge paths are
auto-detected from the running session's marker. Sessions started from
different project directories keep independent skill injections — the marker,
lock, and injected skill are all per-directory, so they never block or attach
to each other — but they do share one MCP server.

If the wrapper is killed hard (`kill -9`), the leftover state is detected and
repaired on the next `wrap`, or clean it manually with `recall-engine unwrap`.

Skill trigger reliability is higher on sonnet-tier models than on haiku
(empirical observation from real-session testing).

## Shared MCP server

The knowledge base is served over MCP by a single streamable-HTTP server per
machine, bound to `127.0.0.1` on an ephemeral port. It is built on the `mcp`
Python SDK 2.x and is stateless, so it serves both the handshake-era protocol
revisions (2024-11-05 through 2025-11-25, which current agent CLIs speak) and
the per-request 2026-07-28 revision. It exposes one read-only tool, one
read-only resource, and one read-only resource template:

- `search_knowledge(query)` — matching note paths with snippets and a
  `resource_uri` for each matching note.
- `recall://notes/index` — exact resource with an index of Markdown notes for
  the selected knowledge paths.
- `recall://note/{encoded_path}` — resource template for one Markdown note
  addressed by the `resource_uri` returned from search results.

`search_knowledge` is a case-insensitive literal substring search over
`<path>/**/*.md` for every selected knowledge path (hidden dot-files and
dot-dirs are skipped; a symlink under a path is a note wherever it points, so
an external file or directory can be mounted into it). Results from all paths
are merged, deduplicated, and ordered by absolute path. It runs
[`ugrep`](https://ugrep.com) when it is on PATH
(`sudo apt install ugrep` / `brew install ugrep`); without it the search still
works, on a slower built-in scan, and `wrap` says so at startup. Nothing is
pre-indexed, so there is no index to keep in sync — a search always reads the
notes as they are on disk.

The first `wrap` spawns the server; later wraps reuse it; the last session to
exit stops it. A state file (`/tmp/recall-engine-mcp-<uid>.json`, guarded by an
flock) records the server's pid, port, token, and the wrap pids that depend on
it. A stale entry left by `kill -9` is detected and replaced on the next `wrap`.

One server serves every session. Each project's injected MCP config carries an
`X-Recall-Paths` header naming its knowledge paths (a JSON array of absolute
directory paths), so the server routes each connection to the right notes; an
`X-Recall-Token` header rejects clients the supervisor did not configure. Two
sessions in different directories with different knowledge paths therefore
share a server yet only ever see their own notes.

On the 2026-07-28 revision, list results (`tools/list`, `resources/list`,
`resources/templates/list`) carry a one-hour public cache hint, since they never
change while the server runs. Note reads keep `ttlMs: 0` and a private scope:
notes change on disk and depend on the caller's knowledge paths.

Two agents need extra setup to reach the server:

- **pi** needs pi >= 0.99, whose built-in MCP reads the injected
  `.pi/mcp.json` only in trusted projects; the first `pi` run in a project
  prompts you to trust it. The entry sets `exposure: "direct"` so
  `search_knowledge` sits in pi's own tool list. The `pi-mcp-adapter`
  extension is optional: when installed it reads the same file, but asks you
  to approve the project server before connecting, and again whenever the
  entry changes (a freshly spawned server has a new port and token).
- **codex** reads the injected `.codex/config.toml` only in trusted projects;
  the first `codex` run in a project prompts you to trust it. Until then codex
  falls back to the shared `~/.codex/config.toml` and will not see the
  recall-engine server.

Skill injection is what guarantees the agent searches before replying; the MCP
tool is how it searches, and MCP resources are the list/read interface. The
skill is the reliable trigger because its text enters the context directly,
whereas an MCP server's `instructions` field is injected at the client's
discretion.

## Options

`wrap` and `doctor` both take the same option. For `wrap`, pass it before
`<agent>`: everything after `<agent>` goes to the agent CLI verbatim.

| Option | Purpose |
|---|---|
| `--knowledge-path` | Path to an existing local knowledge directory; its notes are found under `<path>/**/*.md`. Repeat the option for multiple directories. Optional for a later `wrap` in a directory that already has a running session, where the knowledge paths are auto-detected |

## Doctor

```bash
recall-engine doctor --knowledge-path ~/workspace/notes
```

Checks the supported agent CLIs on PATH (at least one of
claude/codex/pi/gemini/opencode/agy must be present; missing ones print
`[skip]`), the `mcp` package, the shared MCP server, and each `--knowledge-path`
(one `[ok]` / `[fail]` line per path), printing `[ok]` / `[fail]` per check
with a fix instruction for each failure. Exits non-zero if any required check
fails.

The MCP server check reports `[skip]` when no server is running (normal — `wrap`
starts one on demand), `[ok]` with its URL and owner count when it is reachable,
and `[fail]` when the state file records a server that is not, which
`recall-engine unwrap` clears.

## Manual E2E checklist

### wrap

1. In a scratch project, run
   `recall-engine wrap --knowledge-path <dir-a> --knowledge-path <dir-b> <agent>`
   (repeat for each installed agent: claude, codex, pi, gemini, opencode, agy).
   Verify `/tmp/recall-engine-mcp-<uid>.json` appears with this wrap's pid in
   `owners`, and that the agent's config file gained a `recall-engine` entry
   with an `X-Recall-Paths` header listing both directories.
2. Send a message covered by a file in each of `<dir-a>` and `<dir-b>`; observe
   the agent calls `search_knowledge`, reads the returned `resource_uri`, and
   cites the returned note paths.
3. Send an ordinary message not tied to any note; observe the agent still
   searches first and, finding nothing, says so before answering
   from general knowledge. A bare greeting (e.g. "hi") may skip the search.
4. From a second directory, `wrap` another agent against *different* knowledge
   paths. Verify the state file's `owners` grows and the port is unchanged (the
   server was reused), and that queries there only match the second session's
   notes.
5. Quit the first agent; the server keeps running for the second. Quit the
   second; verify the server exits and the state file is removed.
6. Verify `.agents/skills/`, `.claude/skills/`, `.gemini/skills/`,
   `.pi/skills/`, and `.opencode/skills/` contain no `recall-engine`
   leftovers, and that each agent's MCP config is back to its original content.
7. `kill -9` a wrap session, then run `recall-engine doctor` and `wrap` again;
   verify the stale state is reported and then reclaimed.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```
