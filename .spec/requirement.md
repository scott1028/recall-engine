# Recall Engine - BDD Requirements

## Overview

`recall-engine` is a Python CLI that lets an agent CLI (`claude`,
`codex`, `pi`, `gemini`, `opencode`, or `agy`) use one or more local Markdown
knowledge directories.

The tool resolves the configured knowledge paths, starts (or reuses) a shared
MCP server that serves their notes as read-only tools, injects a temporary
Agent Skills skill into the current project (SSOT at `.agents/skills/`, with
symlinks for the other agents' skills dirs), registers the server in the
agent's own MCP config, launches the agent, and restores the project after the
session ends.

The skill is the trigger ("search before replying"); the MCP tools are the
mechanism. Because the server reads the knowledge paths directly,
out-of-project directories work without granting a sandboxed agent filesystem
access to them.

## Current Codebase Facts

- The project is a Python 3.10+ package.
- The package uses a `src/` layout with import package `recall_engine`.
- The CLI entry point is `recall-engine`.
- The CLI is built with Typer.
- The MCP server is built on the `mcp` SDK 2.x (`MCPServer`, stateless
  streamable HTTP) and serves both the handshake-era protocol revisions and the
  2026-07-28 revision.
- The implemented commands are `wrap`, `unwrap`, and `doctor`, plus a
  hidden `mcp-serve` used internally to spawn the shared server.
- The project uses `uv run pytest` and `uv run ruff check src tests` for checks.
- The knowledge search shells out to `ugrep` when it is on `PATH`; without it an
  equivalent built-in Python scan runs. No search index is built or maintained.
- No JavaScript, TypeScript, webpack, Vite, or Next.js config is present.

## Feature: Configure Knowledge Paths

The CLI must resolve one or more knowledge paths before it can wrap an agent.
It is configured entirely through the repeatable `--knowledge-path` CLI option,
which `wrap` and `doctor` both accept.

Scenario: pass wrap's options before the agent name
- Given `wrap` forwards everything after AGENT to the agent CLI verbatim
- When the user configures a wrap session
- Then the options must be passed before AGENT
  (`recall-engine wrap --knowledge-path <path> claude --resume`)

Scenario: use multiple knowledge paths
- Given `--knowledge-path` is passed once for each existing directory
- When the user runs a command that needs the knowledge paths
- Then the CLI uses every given directory as a knowledge root
- And downstream behavior receives the resolved absolute paths, in the given
  order, with duplicates dropped

Scenario: expand and validate each path
- Given a `--knowledge-path` value begins with `~`
- When the CLI resolves configuration
- Then the value is expanded and resolved to an absolute path
- And a value that is not an existing directory fails with a configuration
  error naming that path

Scenario: inherit the knowledge paths from a live wrap session
- Given a wrap session is already running in the same project directory
- And `--knowledge-path` is not passed
- When the user starts another `recall-engine wrap <agent>` there
- Then the CLI reuses the running session's knowledge paths
- And it attaches to the existing skill injection

Scenario: reject missing knowledge path settings
- Given `--knowledge-path` is not passed
- And no live wrap session exists in the current directory
- When the CLI resolves configuration
- Then the command fails with a configuration error

## Feature: Wrap an Agent CLI

`wrap <agent>` must launch a supported agent CLI (`claude`, `codex`, `pi`,
`gemini`, `opencode`, or `agy`) with a temporary knowledge skill.

Scenario: launch a supported agent with knowledge context
- Given valid knowledge paths are configured
- And the agent command exists in the user's shell environment
- When the user runs `recall-engine wrap <agent>` with one of the
  six supported names
- Then the CLI injects the knowledge skill (see the skill feature below)
- And it sets `RECALL_REPO_PATH` to the first resolved knowledge path
- And it launches the agent through the user's interactive shell
- And it exits with the agent's exit code

Scenario: report the resolved knowledge paths
- Given the knowledge paths resolve successfully
- When `wrap` prepares the session
- Then it prints one line per resolved knowledge path

Scenario: classify a wrapper command by version output
- Given a command such as `claude-company` whose `--version` output contains
  a known agent signature (`Claude Code` for claude, `codex` for codex)
- When the user runs `recall-engine wrap claude-company`
- Then the CLI treats it as a wrapper of that agent family and launches it

Scenario: classify a wrapper command by name token
- Given a command such as `gemini-company` whose `--version` exits `0` but
  prints only a bare version number
- When the CLI classifies the command
- Then it splits the command name on `-`, `.`, and `_`
- And a token equal to a supported agent name selects that family
- And a name like `pip` must not classify as `pi`

Scenario: reject unsupported agents
- Given the user runs `recall-engine wrap notclaude`
- And the command matches no agent signature or name token
- When the CLI validates the agent name
- Then the command exits with code `2`
- And the output lists the supported agents

Scenario: prefer a shell function over a binary of the same name
- Given the user defines the agent as a shell function (e.g. an alias wrapper)
- And a binary of the same name is also on `PATH`
- When the CLI launches the agent
- Then the shell function wins, because the agent runs through the user's shell

Scenario: refuse an unsafe agent name
- Given the agent is launched through the user's interactive shell
- When the agent name contains shell metacharacters (e.g. `x; rm -rf ~`)
- Then the CLI refuses to classify or launch it
- And nothing is passed to the shell

Scenario: restore after launch failure
- Given the skill was injected
- And the agent cannot be launched
- When the launcher fails
- Then the CLI restores the project skill state before exiting

Scenario: restore when the session is interrupted or the terminal closes
- Given a wrap session is running the agent
- When the user presses Ctrl-C (SIGINT) or closes the terminal (SIGHUP)
- Then the signal is forwarded to the agent and the wrapper stays alive
- And it tears down its injected skill and MCP config before exiting

## Feature: Inject and Restore the Skill

The skill injection must be reversible and safe around interrupted sessions.

Scenario: inject the rendered skill at the Agent Skills SSOT
- Given the CLI has resolved the knowledge paths
- When the wrapper injects the skill
- Then it writes `.agents/skills/recall-engine/SKILL.md`
- And the skill directs the agent to the MCP tools, naming no knowledge path
- And it writes `.agents/skills/.recall-engine-marker.json`
- And the marker records the resolved knowledge paths so a later wrap in the
  same directory can reuse them without re-specifying `--knowledge-path`

Scenario: skip an agent dir that already resolves to the SSOT
- Given an agent's skills dir is itself a symlink to `.agents/skills/`
- When the wrapper injects its skill
- Then it creates no symlink there, since the skill is already reachable
- And it never creates a symlink that points at itself

Scenario: symlink the skill into the other agents' skills dirs
- Given the skill was written to `.agents/skills/recall-engine/`
- When the wrapper finishes injecting
- Then `.claude/skills/recall-engine`,
  `.gemini/skills/recall-engine`,
  `.pi/skills/recall-engine`, and
  `.opencode/skills/recall-engine` are relative symlinks to it
- And `codex` and `agy` read `.agents/skills/` directly, so they get no symlink
- And all symlinks are removed when the session ends

Scenario: register the MCP server in the agent's config
- Given the shared MCP server is running
- When the wrapper injects the MCP config for the launched agent
- Then the agent's own config file gains a `recall-engine` server entry
  (`.mcp.json` for claude, `.codex/config.toml` for codex, `.pi/mcp.json` for
  pi, `.gemini/settings.json` for gemini, `opencode.json` for opencode, and
  `.agents/mcp_config.json` for agy)
- And the opencode entry uses the opencode v2 shape (`mcp.servers.<name>`,
  no `enabled` field), which opencode v1 cannot read
- And the pi entry sets `exposure: "direct"`, so pi's built-in MCP (and
  pi-mcp-adapter, which maps it to `directTools`) lists the search tool directly
- And the entry carries the server URL, an `X-Recall-Paths` header holding a
  JSON array of the resolved knowledge paths, and an `X-Recall-Token` header
- And the entry is removed when the session ends

> Note: codex reads its `.codex/config.toml` only for trusted projects; an
> untrusted project must be trusted (first-run prompt) before codex sees the
> entry. Verified: codex 0.144.1 loads the project-local file and forwards the
> custom headers.

Scenario: preserve an existing MCP config
- Given the agent's MCP config file already exists with other servers
- And it was not created by the active wrapper session
- When the wrapper injects its server entry
- Then only the `recall-engine` entry is added
- And the file's original content is restored after the session ends

Scenario: tolerate a commented or empty pre-existing config
- Given the agent's config file exists but is empty or contains JSONC comments
  (gemini/opencode accept `//` and `/* */` comments)
- When the wrapper injects its server entry
- Then injection succeeds
- And the file is restored byte-identical when the session ends

Scenario: patch an outdated entry on a new wrap
- Given an earlier version injected the `recall-engine` entry without a field
  the current version adds (e.g. pi's `exposure`)
- When a new wrap session registers the MCP server
- Then the entry is re-asserted so the missing field is patched in
- And no duplicate backup is created for an already-registered config

Scenario: refuse an unparseable pre-existing config
- Given the agent's config file is malformed (invalid JSON/TOML) or is not an
  object/table
- When the wrapper tries to inject its server entry
- Then it fails with a clear message naming the file and exits `1`
- And the original file is left untouched with no stray backup
- And the skill it already injected is torn down

Scenario: preserve an existing user skill
- Given `recall-engine/` already exists in the SSOT dir or in an
  agent-specific skills dir
- And it was not created by the active wrapper session
- When the wrapper injects its skill
- Then the existing entry is moved to a backup location
- And the original entry is restored after the session ends

Scenario: attach to a live wrapper session in the same directory
- Given the marker file records a live owner and the skill is injected
- When another wrap session starts in the same directory with the same
  knowledge paths
- Then it attaches by adding its process id to the marker
- And it reuses the existing injection instead of re-injecting
- And the last session to exit tears the injection down

Scenario: refuse attach on different knowledge paths
- Given a live wrapper session for one set of knowledge paths
- When another wrap session starts in the same directory for a different
  list of knowledge paths
- Then it fails without disturbing the existing session

Note: wrap sessions in different directories are independent; the marker,
lock, and injected skill are all anchored per project directory, so sessions
started from separate directories never block or attach to each other.

Scenario: recover stale wrapper state
- Given the marker file records a dead process id
- When a new wrap session starts
- Then the CLI restores the stale skill state first
- And the new wrap session may continue

Scenario: manually clean stale wrapper state
- Given leftover wrapper state exists
- When the user runs `recall-engine unwrap`
- Then the CLI restores or removes the injected skill state
- And the output reports whether anything was cleaned

Scenario: clean leftovers from a pre-SSOT version
- Given a marker from an older version exists at
  `.claude/skills/.recall-engine-marker.json`
- And no marker exists at the SSOT location
- When the CLI restores skill state
- Then the legacy skill state is cleaned as well

## Feature: Share One MCP Server Across Sessions

One streamable-HTTP MCP server per machine serves every wrap session. It
exposes one read-only tool, `search_knowledge(query)`, plus two read-only
resources — `recall://notes/index` (the note list) and
`recall://note/{encoded_path}` (one note's text) — and is started on demand
rather than run as a user-managed daemon.

Scenario: start the server on the first wrap
- Given no recall-engine MCP server is running
- When the user runs `recall-engine wrap <agent>`
- Then the CLI spawns a detached server bound to `127.0.0.1` on an ephemeral port
- And it records the pid, port, url, token, and owner pid in
  `/tmp/recall-engine-mcp-<uid>.json`
- And it waits until the server accepts connections before launching the agent

Scenario: fail loudly when the server cannot start
- Given the spawned server process dies before it accepts connections
- When `wrap` waits for it to become healthy
- Then the wait fails instead of hanging or launching the agent against a dead
  server

Scenario: serve both protocol eras
- Given the shared server is running
- When a client connects with the initialize handshake (revisions 2024-11-05
  through 2025-11-25)
- Then the handshake succeeds and every tool and resource works
- And when a client instead probes `server/discover` and speaks the per-request
  2026-07-28 revision, the same tool and resources work without a session
- And `X-Recall-Paths` / `X-Recall-Token` routing behaves the same in both eras
- And on 2026-07-28, list results carry a one-hour public cache hint, while
  note reads stay `ttlMs: 0` and private because notes change on disk

Scenario: reuse the server for a later wrap
- Given a recall-engine MCP server is already running and reachable
- When a second `wrap` starts, in any directory and with any knowledge paths
- Then no second server is spawned
- And the second session's pid is added to the state file's `owners`

Scenario: route each connection to its own knowledge paths
- Given two wrap sessions share one server with different knowledge paths
- When an agent calls `search_knowledge`
- Then the server resolves the roots from the connection's `X-Recall-Paths`
  header (a JSON array of absolute directory paths)
- And results only ever come from that session's roots

Scenario: reject an unconfigured client
- Given the server was started with a token
- When a client calls a tool without the matching `X-Recall-Token` header
- Then the tool call fails with an authorization error
- And a call whose `X-Recall-Paths` header is missing, is not a non-empty JSON
  array of strings, or names a directory that does not exist is rejected the
  same way

Scenario: refuse to read a note outside the knowledge paths
- Given `recall://note/{encoded_path}` addresses one note by its encoded
  absolute path
- When the encoded path is malformed, or decodes to a path that escapes every
  configured root (e.g. an absolute path outside them, or any path containing
  `..`)
- Then the read fails, so no path the session does not itself expose is ever read
- And any `..` is rejected outright, since a symlinked directory makes the
  lexical parent differ from the real one
- And a note the session does expose is still served, including one symlinked
  out of a root — see "agree on what counts as a note"

Scenario: stop the server with the last session
- Given two sessions own the running server
- When the first session exits
- Then the server keeps running and only that pid leaves `owners`
- And when the last owner exits, the server is terminated
- And the state file is removed

Scenario: reclaim a stale server record
- Given the state file records a server whose process is dead or not listening
- When the user runs the next `wrap`
- Then the stale record is replaced by a freshly spawned server

Scenario: reach the server from pi
- Given `pi` >= 0.99, whose built-in MCP reads `.pi/mcp.json` in trusted projects
- When the user runs `recall-engine wrap pi`
- Then `wrap` launches pi whether or not `pi-mcp-adapter` is installed
- And `doctor` notes that pi needs >= 0.99 and a trusted project

## Feature: Search the Knowledge Base

`search_knowledge(query)` is a case-insensitive, literal (non-regex) substring
search over `<path>/**/*.md` for every configured knowledge path. `ugrep`
performs it when installed; a built-in scan performs it otherwise. Both must
answer identically — ugrep is an accelerator, never a different feature.

Scenario: match notes by substring
- Given the selected knowledge paths contain notes under them
- When the agent calls `search_knowledge` with a query
- Then each matching line is returned with its note path, line number, snippet,
  and the `resource_uri` of the note it came from
- And matches from all roots are merged, deduplicated by (path, line), and
  ordered by note path, then by line number
- And matching is case-insensitive, so `abc` matches `AbC`
- And the query is a literal string, so `a.b` does not match `axb`
- And at most 50 matches are returned

Scenario: search with ugrep installed
- Given `ugrep` is on `PATH`
- When the user runs `recall-engine wrap <agent>`
- Then the CLI reports the ugrep binary the search will use
- And `search_knowledge` answers through ugrep

Scenario: search without ugrep installed
- Given `ugrep` is not on `PATH`
- When the user runs `recall-engine wrap <agent>`
- Then the CLI warns that search falls back to a slower built-in scan and says
  how to install ugrep
- And the agent still launches
- And `search_knowledge` still returns the same matches, through the scan

Scenario: nested roots do not duplicate results
- Given one configured knowledge path is a subdirectory of another
- When the agent calls `search_knowledge`
- Then a note reachable through both roots is reported once

Scenario: agree on what counts as a note
- Given a knowledge path contains a hidden note (a dot-file or a note under a
  dot-directory), a symlink to a note inside the path, a symlink to a file
  outside it, a symlink to a directory outside it, a symlink back to one of its
  own ancestors, and a note whose bytes are not valid UTF-8
- When the agent calls `search_knowledge`
- Then hidden notes are never searched
- And a symlink under a knowledge path is a note wherever it points: containment
  is judged by where the symlink itself sits, not by what it resolves to, so an
  external file mounted under the path is searched
- And a symlinked directory is descended into, so an external directory of
  notes can be mounted under a knowledge path
- And a symlink back to an ancestor is not followed, so a loop cannot recurse
  forever
- And the note that is not valid UTF-8 is still searched, with the undecodable
  bytes replaced in the snippet
- And the answer is identical whether or not ugrep is installed

Scenario: list only what search can find
- Given the same definition of a note governs search and the note list
- When the agent reads `recall://notes/index`
- Then it lists exactly the notes `search_knowledge` is able to match
- And hidden notes appear in neither
- And each entry carries the note's absolute `path`, its `relative_path` under
  its own root, and the `resource_uri` for reading it
- And notes with the same relative path under different roots get distinct
  resource URIs, because the URI encodes the absolute path

Scenario: always search the notes as they are on disk
- Given a wrap session is running
- And a note is added, edited, or deleted under a knowledge path
- When the agent next calls `search_knowledge`
- Then the results reflect the current files on disk
- And no index has to be rebuilt, reconciled, or invalidated for that to hold

## Feature: Use the Knowledge Base During Agent Sessions

The injected skill must guide the agent to consult the Markdown knowledge base
of existing processing records, notes, and prior experience before replying to
any user message — especially messages about troubleshooting, past work,
lessons learned, decisions, or terminology.

Scenario: answer with knowledge-base context
- Given an agent is running through `recall-engine wrap <agent>`
- And the user sends any message
- When the agent prepares the reply
- Then the skill instructs the agent to call `search_knowledge` (and to read
  `recall://notes/index` / `recall://note/{encoded_path}` as needed) for existing
  processing records, notes, or prior handling of similar problems
- And any prior experience found should be factored into the reply and cite the
  note path returned by the tools

Scenario: answer when no matching note exists
- Given the agent called `search_knowledge`
- And no relevant note was found
- When the agent answers the user
- Then it should say no relevant knowledge-base entry was found
- And it may continue with general knowledge

Scenario: skip the search for a trivial message
- Given the user sends a bare greeting or acknowledgement with no
  searchable keywords (e.g. "hi", "thanks")
- When the agent prepares the reply
- Then the skill allows a reply without searching the knowledge base

Scenario: avoid unintended edits
- Given the agent uses the knowledge paths as reference material
- When the user has not asked to edit them
- Then the agent must not modify knowledge base files

## Feature: Diagnose Local Setup

`doctor` must report local readiness one check at a time.

Scenario: all required checks pass
- Given at least one agent CLI and at least one valid `--knowledge-path` are
  available
- When the user runs `recall-engine doctor`
- Then each required check prints `[ok]`
- And the command exits successfully

Scenario: report each knowledge path individually
- Given the user passes one or more `--knowledge-path` values
- When the user runs `doctor`
- Then each existing directory prints `[ok]` on its own line
- And each missing directory prints `[fail]` naming that path

Scenario: no knowledge paths configured
- Given `--knowledge-path` is not passed
- When the user runs `doctor`
- Then the knowledge paths check prints `[fail]`
- And the fix instruction names `--knowledge-path`
- And the command exits with code `1`

Scenario: report each agent CLI individually
- Given some of `claude`, `codex`, `pi`, `gemini`, `opencode`, and `agy` are installed
- When the user runs `doctor`
- Then each installed agent prints `[ok]`
- And each missing agent prints `[skip]`
- And the agent check passes when at least one agent is installed

Scenario: no agent CLI installed
- Given none of `claude`, `codex`, `pi`, `gemini`, `opencode`, and `agy` is on `PATH`
- When the user runs `doctor`
- Then the agent check prints `[fail]`
- And the output includes install instructions for the supported agents
- And the command exits with code `1`

Scenario: a required check fails
- Given any required dependency is missing or invalid
- When the user runs `doctor`
- Then that check prints `[fail]`
- And the output includes a concrete fix instruction
- And the command exits with code `1`

Scenario: report the search backend
- Given the user runs `recall-engine doctor`
- When the ugrep check runs
- Then it prints `[ok]` with the binary path when ugrep is installed
- And it prints `[skip]` naming the built-in-scan fallback when it is not
- And that skip does not make doctor fail, because ugrep is optional

Scenario: report a reachable MCP server
- Given a wrap session has started the shared MCP server
- When the user runs `doctor`
- Then the MCP server check prints `[ok]` with the server URL and owner count

Scenario: no MCP server is running
- Given no wrap session is active
- When the user runs `doctor`
- Then the MCP server check prints `[skip]`
- And that skip does not make doctor fail

Scenario: report a stale MCP server record
- Given the state file records a server that is not reachable
- When the user runs `doctor`
- Then the MCP server check prints `[fail]`
- And the fix instruction names `recall-engine unwrap`
- And the command exits with code `1`

## Out of Scope for v1

- LLM proxying or traffic interception
- A user-managed daemon lifecycle (`start` / `stop` commands, autostart on
  boot). The shared MCP server is in scope, but it is spawned on demand by
  `wrap` and stops with the last session.
- CLI aliases such as `recall` or `rengine`
- Agent support beyond `claude`, `codex`, `pi`, `gemini`, `opencode`, and `agy`

## Acceptance Checks

Automated:
- `uv run pytest`
- `uv run ruff check src tests`

Manual:
- `recall-engine --help` lists `wrap`, `unwrap`, and `doctor`, and
  documents the ugrep search backend and its fallback
- `recall-engine doctor` reports ugrep as `[ok]` when installed and `[skip]`
  (without failing) when it is not
- `recall-engine doctor --knowledge-path <a> --knowledge-path <b>` prints one
  `[ok]` line per existing directory
- A wrapped agent session reads and cites a relevant file from each configured
  knowledge path
