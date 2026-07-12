# recall-engine

`recall-engine` is a thin launcher that wraps an agent CLI (`claude`,
`codex`, `pi`, `gemini`, `opencode`, or `agy`) with an existing local Markdown
knowledge base. It prepares the knowledge repo, starts
(or reuses) a shared MCP server that serves the repo's notes, injects a skill
that makes the agent consult those notes — its record of past processing and
notes — before replying to any message, points the agent's MCP config at the
server, launches the agent, and restores everything on exit. It also ships a
one-way sync between a Google Drive folder and the repo's `src/` directory.

The knowledge base is reached through an MCP tool and resources rather than the
filesystem, so the repo can live outside the project without a sandboxed agent
needing access to it.

## Install

From a checkout of this repository:

```bash
uv tool install .
# or
pipx install .
```

Requirements: Python >= 3.10, `git`, and at least one supported agent CLI:
Claude Code (https://claude.com/claude-code), Codex CLI
(https://developers.openai.com/codex/cli), pi
(https://github.com/earendil-works/pi), Gemini CLI
(https://github.com/google-gemini/gemini-cli), opencode, or agy. The install
provides the `recall-engine` entry point.

## Quickstart

```bash
# local knowledge repo
recall-engine wrap --local-knowledge-path ~/workspace/recall claude

# other agent CLIs
recall-engine wrap --local-knowledge-path ~/workspace/recall codex
recall-engine wrap --local-knowledge-path ~/workspace/recall pi
recall-engine wrap --local-knowledge-path ~/workspace/recall gemini
recall-engine wrap --local-knowledge-path ~/workspace/recall opencode
recall-engine wrap --local-knowledge-path ~/workspace/recall agy
```

## How wrap works

`recall-engine wrap [options] <agent> [args...]` runs a six-step lifecycle.
Anything after `<agent>` is forwarded verbatim to the agent — so pass
recall-engine's own options **before** `<agent>` — and leading environment
variables are inherited, so
`AA=1 recall-engine wrap claude foo --bar` behaves like
`AA=1 claude foo --bar`:

1. **Prepare the repo** — `--local-knowledge-path` selects an existing local
   knowledge repo (its notes live under `<path>/src/`), and the command fails if
   the directory is missing.
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
5. **Launch the agent** — as a child process with signals forwarded and
   `RECALL_REPO_PATH` set to the resolved repo path. Unknown command
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
`--local-knowledge-path` — when it is not passed, the repo is auto-detected from
the running session's marker. Sessions started from different project directories
keep independent skill injections — the marker, lock, and injected skill are
all per-directory, so they never block or attach to each other — but they do
share one MCP server.

If the wrapper is killed hard (`kill -9`), the leftover state is detected and
repaired on the next `wrap`, or clean it manually with `recall-engine unwrap`.

Skill trigger reliability is higher on sonnet-tier models than on haiku
(empirical observation from real-session testing).

## Shared MCP server

The knowledge base is served over MCP by a single streamable-HTTP server per
machine, bound to `127.0.0.1` on an ephemeral port. It exposes one read-only
tool, one read-only resource, and one read-only resource template:

- `search_knowledge(query)` — matching note paths with snippets and a
  `resource_uri` for each matching note.
- `recall://notes/index` — exact resource with an index of Markdown notes for
  the selected repo.
- `recall://note/{encoded_path}` — resource template for one Markdown note
  addressed by the `resource_uri` returned from search results.

`search_knowledge` is a case-insensitive literal substring search over
`<repo>/src/**/*.md` (hidden dot-files and dot-dirs are skipped; a symlink under
`src/` is a note wherever it points, so an external file or directory can be
mounted into the repo — Drive sync never follows one). It runs
[`ugrep`](https://ugrep.com) when it is on PATH
(`sudo apt install ugrep` / `brew install ugrep`); without it the search still
works, on a slower built-in scan, and `wrap` says so at startup. Nothing is
pre-indexed, so there is no index to keep in sync — a search always reads the
notes as they are on disk.

The first `wrap` spawns the server; later wraps reuse it; the last session to
exit stops it. A state file (`/tmp/recall-engine-mcp-<uid>.json`, guarded by an
flock) records the server's pid, port, token, and the wrap pids that depend on
it. A stale entry left by `kill -9` is detected and replaced on the next `wrap`.

One server serves every repo. Each project's injected MCP config carries an
`X-Recall-Repo` header naming its knowledge repo, so the server routes each
connection to the right notes; an `X-Recall-Token` header rejects clients the
supervisor did not configure. Two sessions in different directories with
different repos therefore share a server yet only ever see their own notes.

Two agents need extra setup to reach the server:

- **pi** requires the `pi-mcp-adapter` extension (`pi install npm:pi-mcp-adapter`).
  Without it pi cannot reach the server, so `wrap pi` refuses to launch and
  tells you to install the adapter first.
- **codex** reads the injected `.codex/config.toml` only in trusted projects;
  the first `codex` run in a project prompts you to trust it. Until then codex
  falls back to the shared `~/.codex/config.toml` and will not see the
  recall-engine server.

Skill injection is what guarantees the agent searches before replying; the MCP
tool is how it searches, and MCP resources are the list/read interface. The
skill is the reliable trigger because its text enters the context directly,
whereas an MCP server's `instructions` field is injected at the client's
discretion.

## Google Drive sync

### Setup

Drive access uses gcloud credentials with the Drive scope. Either login works:

```bash
# Option A: gcloud user login (token minted via `gcloud auth print-access-token`)
gcloud auth login --enable-gdrive-access

# Option B: Application Default Credentials (the default
# `gcloud auth application-default login` does NOT include the Drive scope)
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform
```

ADC is tried first, then the user login. Some organizations also require a
quota project for ADC:
`gcloud auth application-default set-quota-project <project-id>`.

### Usage

```bash
recall-engine sync download \
  --local-knowledge-path ~/workspace/recall \
  --remote-knowledge-folder Shared   # Drive folder -> <repo>/src/

recall-engine sync upload \
  --local-knowledge-path ~/workspace/recall \
  --remote-knowledge-folder Shared   # <repo>/src/ -> Drive folder
```

`--remote-knowledge-folder` accepts either the Drive folder ID or the folder
name. Name matching is case-insensitive; if several folders share the name, sync
aborts and asks for the folder ID.

### Behavior notes

- Sync is **strictly one-way** per invocation; there is no merge.
- Files are **overwritten by name** (last writer wins) — the knowledge repo is
  a git repo, so `git diff` / `git checkout` are your safety net after a
  download.
- **Deletes never propagate**: removing a file on one side does not remove it
  on the other.
- Native Google Docs are **exported to `.md`** on download (verify the exported
  content quality yourself — tables, images, and formatting depend on Google's
  exporter).
- Upload never converts `.md` files back to native Google Docs; they stay
  plain Markdown files on Drive.
- Sync is **non-recursive**: only `<repo>/src/*.md` and the top level of the
  Drive folder are considered.

### Auto-sync on first wrap

When `wrap` brings a repo online for the **first time on the machine** (no other
live wrap session is using that repo) and `--remote-knowledge-folder` is passed,
it fires a one-time `sync download` in a detached background subprocess so you start
with the latest notes. It is **best-effort**: the wrap never waits on it and a
failure (missing credentials, folder not found, …) is ignored — the error is
written to the shared server log (`/tmp/recall-engine-mcp-<uid>.log`) instead of
the terminal, so it never disrupts the agent. The repo counts as "first" again
once all of its wrap sessions have exited.

## Options

`wrap`, `sync`, and `doctor` all take the same two options. For `wrap`, pass them
before `<agent>`: everything after `<agent>` goes to the agent CLI verbatim.

| Option | Purpose |
|---|---|
| `--local-knowledge-path` | Path to an existing local knowledge repo; its notes live under `<path>/src/`. Optional for a later `wrap` in a directory that already has a running session, where the repo is auto-detected |
| `--remote-knowledge-folder` | Google Drive folder ID or name (case-insensitive) for `sync download` / `sync upload` and the first-wrap auto-download |

## Doctor

```bash
recall-engine doctor
```

Checks git, the supported agent CLIs on PATH (at least one of
claude/codex/pi/gemini/opencode/agy must be present; missing ones print
`[skip]`), the `mcp` package, the shared MCP server, repo
configuration, and gcloud Drive access, printing `[ok]` / `[fail]` per check
with a fix instruction for each failure. Exits non-zero if any required check
fails.

The MCP server check reports `[skip]` when no server is running (normal — `wrap`
starts one on demand), `[ok]` with its URL and owner count when it is reachable,
and `[fail]` when the state file records a server that is not, which
`recall-engine unwrap` clears.

## Manual E2E checklist

### wrap

1. In a scratch project, run
   `recall-engine wrap --local-knowledge-path <sample-repo> <agent>`
   (repeat for each installed agent: claude, codex, pi, gemini, opencode, agy).
   Verify `/tmp/recall-engine-mcp-<uid>.json` appears with this wrap's pid in
   `owners`, and that the agent's config file gained a `recall-engine` entry.
2. Send a message covered by a file in `<sample-repo>/src/`; observe the
   agent calls `search_knowledge`, reads the returned `resource_uri`, and cites
   the returned `src/*.md` path.
3. Send an ordinary message not tied to any note; observe the agent still
   searches first and, finding nothing, says so before answering
   from general knowledge. A bare greeting (e.g. "hi") may skip the search.
4. From a second directory, `wrap` another agent against a *different* repo.
   Verify the state file's `owners` grows and the port is unchanged (the server
   was reused), and that queries there only match the second repo's notes.
5. Quit the first agent; the server keeps running for the second. Quit the
   second; verify the server exits and the state file is removed.
6. Verify `.agents/skills/`, `.claude/skills/`, `.gemini/skills/`,
   `.pi/skills/`, and `.opencode/skills/` contain no `recall-engine`
   leftovers, and that each agent's MCP config is back to its original content.
7. `kill -9` a wrap session, then run `recall-engine doctor` and `wrap` again;
   verify the stale state is reported and then reclaimed.

### drive sync

1. Create a Drive folder with a few `.md` files and a native Google Doc; note its folder ID.
2. Run `sync download`; verify the files landed in `<repo>/src/` and check the exported Doc's `.md` content.
3. Edit a local `.md`, run `sync upload`, and verify the change on Drive.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```
