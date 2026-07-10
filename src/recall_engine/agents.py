"""Registry of supported agent CLIs and their skills conventions."""

from __future__ import annotations

from dataclasses import dataclass

# Agent Skills spec location; the real skill is written here (SSOT) and the
# other agents' skills dirs get symlinks pointing at it.
SKILLS_SSOT_DIR = ".agents/skills"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    skills_dir: str  # project-level skills discovery dir (relative path)
    version_signature: str | None  # lowercase substring of `--version` stdout
    install_hint: str


AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        name="claude",
        skills_dir=".claude/skills",
        version_signature="claude code",
        install_hint="install Claude Code: https://claude.com/claude-code",
    ),
    "codex": AgentSpec(
        name="codex",
        skills_dir=SKILLS_SSOT_DIR,
        version_signature="codex",
        install_hint="install Codex CLI: https://developers.openai.com/codex/cli",
    ),
    "pi": AgentSpec(
        name="pi",
        skills_dir=".pi/skills",
        version_signature=None,  # `pi --version` prints a bare version number
        install_hint="install pi: https://github.com/earendil-works/pi",
    ),
    "gemini": AgentSpec(
        name="gemini",
        skills_dir=".gemini/skills",
        version_signature=None,  # `gemini --version` prints a bare version number
        install_hint="install Gemini CLI: https://github.com/google-gemini/gemini-cli",
    ),
    "opencode": AgentSpec(
        name="opencode",
        skills_dir=".opencode/skills",  # opencode reads only .opencode/skills per project
        version_signature=None,  # `opencode --version` prints a bare version number
        install_hint="install the opencode CLI",
    ),
    "agy": AgentSpec(
        name="agy",
        skills_dir=SKILLS_SSOT_DIR,  # agy reads .agents/skills directly
        version_signature=None,  # `agy --version` prints a bare version number
        install_hint="install the agy CLI",
    ),
}
