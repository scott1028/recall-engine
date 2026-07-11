import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from recall_engine.skill import SkillError, detect_active_repo, inject_skill, restore_skill
# Skills dirs that receive a symlink to the SSOT skill in .agents/skills/.
# codex and agy read .agents/skills directly, so they are not listed here.
LINK_SKILLS_DIRS = (".claude/skills", ".gemini/skills", ".pi/skills", ".opencode/skills")
@pytest.fixture
def project(tmp_path, monkeypatch):
    """Run each test inside a fresh project cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
def skill_paths(project: Path) -> tuple[Path, Path, Path]:
    skills = project / ".agents" / "skills"
    return (skills / "recall-engine", skills / ".recall-engine-marker.json", skills / ".recall-engine-backup")
def link_paths(project: Path) -> list[Path]:
    return [project / skills_dir / "recall-engine" for skills_dir in LINK_SKILLS_DIRS]
def dead_pid() -> int:
    """Spawn a short-lived process and return its pid after it exits."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid
def test_inject_creates_skill_marker_symlinks_and_public_content(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)
    skill_dir, marker, _ = skill_paths(project)
    content = (skill_dir / "SKILL.md").read_text()
    assert content.startswith("---\n")
    frontmatter = content.split("---")[1]
    assert "name: recall-engine" in frontmatter
    description_lines = [line for line in frontmatter.splitlines() if line.startswith("description:")]
    assert len(description_lines) == 1
    description = description_lines[0].removeprefix("description:").strip()
    assert "every conversation" in description
    assert "before any response" in description
    assert "search_knowledge" in content
    assert (
        "Before calling `search_knowledge` or reading `recall://notes/index`, "
        "predict useful expansion keywords"
    ) in content
    assert "Split compound phrases into shorter search terms" in content
    assert "read_note" not in content
    assert "list_notes" not in content
    assert "recall://note/{encoded_path}" in content
    assert "resource_uri" in content
    assert "{%" not in content
    assert "{{" not in content
    assert "{knowledge_dir}" not in content
    assert "{repo_path}" not in content
    assert ".knowledge" not in content
    record = json.loads(marker.read_text())
    assert record["pids"] == [os.getpid()]
    assert record["backup"] is None
    assert "knowledge" not in record  # no more in-project .knowledge link
    assert record["repo_path"] == str(repo)
    assert record["injected_at"]
    for link in link_paths(project):
        assert link.is_symlink()
        assert not Path(os.readlink(link)).is_absolute()
        assert link.resolve() == skill_dir.resolve()
        assert (link / "SKILL.md").exists()
    knowledge = project / ".knowledge"
    assert not knowledge.exists()
    assert not knowledge.is_symlink()
def test_inject_skips_agent_dir_that_resolves_to_ssot(project, tmp_path):
    claude_skills = project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    os.symlink(os.path.relpath(project / ".agents" / "skills", claude_skills.parent), claude_skills)
    inject_skill(tmp_path / "repo")
    skill_dir, _, backup = skill_paths(project)
    assert skill_dir.is_dir()
    assert not skill_dir.is_symlink()
    assert (skill_dir / "SKILL.md").exists()
    assert not backup.exists()
    assert not (project / ".claude" / "skills" / "recall-engine").is_symlink()
    assert (project / ".gemini" / "skills" / "recall-engine").is_symlink()
    assert (project / ".pi" / "skills" / "recall-engine").is_symlink()
def test_inject_leaves_existing_knowledge_untouched(project, tmp_path):
    # We no longer manage .knowledge, so a user's own .knowledge is left alone.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    existing = project / ".knowledge"
    existing.mkdir()
    (existing / "user.md").write_text("user knowledge\n")
    inject_skill(repo)
    assert existing.is_dir()
    assert not existing.is_symlink()
    assert (existing / "user.md").read_text() == "user knowledge\n"
    assert restore_skill(owner_pid=os.getpid()) is True
    assert existing.is_dir()
    assert (existing / "user.md").read_text() == "user knowledge\n"
def test_restore_cleans_legacy_knowledge_link_from_old_marker(project, tmp_path):
    # An old-version marker recorded a .knowledge symlink; restore must clean it.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    inject_skill(repo)
    _, marker, _ = skill_paths(project)
    knowledge = project / ".knowledge"
    os.symlink(repo / "src", knowledge)
    record = json.loads(marker.read_text())
    record["knowledge"] = {"path": str(knowledge), "backup": None}
    marker.write_text(json.dumps(record))
    assert restore_skill(owner_pid=os.getpid()) is True
    assert not knowledge.exists()
    assert not knowledge.is_symlink()
def test_restore_with_nothing_then_removes_skill_marker_and_symlinks(project, tmp_path):
    assert restore_skill() is False
    inject_skill(tmp_path / "repo")
    assert restore_skill(owner_pid=os.getpid()) is True
    skill_dir, marker, backup = skill_paths(project)
    assert not skill_dir.exists()
    assert not marker.exists()
    assert not backup.exists()
    for link in link_paths(project):
        assert not link.exists()
        assert not link.is_symlink()
def test_preexisting_user_skill_backed_up_and_restored(project, tmp_path, capsys):
    skill_dir, _, backup = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("user's own skill\n")
    (skill_dir / "extra.txt").write_text("distinctive user file\n")
    inject_skill(tmp_path / "repo")
    assert "moved to backup" in capsys.readouterr().err
    assert (backup / "extra.txt").read_text() == "distinctive user file\n"
    assert "knowledge base" in (skill_dir / "SKILL.md").read_text()
    assert restore_skill(owner_pid=os.getpid()) is True
    assert (skill_dir / "SKILL.md").read_text() == "user's own skill\n"
    assert (skill_dir / "extra.txt").read_text() == "distinctive user file\n"
    assert not backup.exists()
def test_preexisting_agent_dir_skill_backed_up_and_restored(project, tmp_path, capsys):
    # A user's own skill in an agent-specific dir (e.g. .claude/skills) must
    # survive the wrap session even though the path becomes a symlink.
    user_skill = project / ".claude" / "skills" / "recall-engine"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("user's claude skill\n")
    inject_skill(tmp_path / "repo")
    assert "moved to backup" in capsys.readouterr().err
    assert user_skill.is_symlink()
    assert restore_skill(owner_pid=os.getpid()) is True
    assert not user_skill.is_symlink()
    assert (user_skill / "SKILL.md").read_text() == "user's claude skill\n"
def test_stale_marker_dead_pid_auto_restored(project, tmp_path, capsys):
    skill_dir, marker, _ = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("leftover from a killed wrapper\n")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": dead_pid(), "backup": None}))
    repo = tmp_path / "repo"
    inject_skill(repo)
    assert "stale wrap session" in capsys.readouterr().err
    assert "search_knowledge" in (skill_dir / "SKILL.md").read_text()
    assert json.loads(marker.read_text())["pids"] == [os.getpid()]
def test_legacy_markers_without_links_and_ssot_alias_restore(project):
    # Markers written before symlink support carried no "links" entry.
    skill_dir, marker, _ = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("leftover\n")
    marker.write_text(json.dumps({"pid": dead_pid(), "backup": None}))
    assert restore_skill() is True
    assert not skill_dir.exists()
    assert not marker.exists()
    claude_skills = project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    os.symlink(os.path.relpath(project / ".agents" / "skills", claude_skills.parent), claude_skills)
    skill_dir, marker, backup = skill_paths(project)
    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    backup.mkdir()
    (backup / "SKILL.md").write_text("restored user skill\n")
    os.symlink("../../.agents/skills/recall-engine", skill_dir)
    marker.write_text(json.dumps({"pids": [dead_pid()], "backup": str(backup), "links": [{"path": str(project / ".claude" / "skills" / "recall-engine"), "backup": str(project / ".claude" / "skills" / ".recall-engine-backup")}]}))
    assert restore_skill(force=True) is True
    assert not marker.exists()
    assert skill_dir.is_dir()
    assert not skill_dir.is_symlink()
    assert (skill_dir / "SKILL.md").read_text() == "restored user skill\n"
    assert not backup.exists()
def test_legacy_claude_skills_marker_cleaned(project):
    # Pre-SSOT sessions left their state under .claude/skills/.
    legacy = project / ".claude" / "skills"
    legacy_skill = legacy / "recall-engine"
    legacy_skill.mkdir(parents=True)
    (legacy_skill / "SKILL.md").write_text("legacy leftover\n")
    (legacy / ".recall-engine-marker.json").write_text(json.dumps({"pid": dead_pid(), "backup": None}))
    assert restore_skill() is True
    assert not legacy_skill.exists()
    assert not (legacy / ".recall-engine-marker.json").exists()
def live_pid():
    """Spawn a long-lived process; caller must terminate it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
def test_live_session_attach_refcount_and_repo_guard(project, tmp_path):
    # A pre-existing user skill must survive until the LAST owner leaves.
    skill_dir, marker, backup = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("user's own skill\n")
    repo = tmp_path / "repo-a"
    inject_skill(repo)  # backs up user skill; pids == [me]
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        inject_skill(repo)  # same dir, same repo -> attach, no re-inject
        pids = json.loads(marker.read_text())["pids"]
        assert other.pid in pids and os.getpid() in pids
        assert backup.exists()
        with pytest.raises(SkillError, match="different"):
            inject_skill(tmp_path / "repo-b")
        # First owner (us) leaves while `other` is still alive -> keep injection.
        assert restore_skill(owner_pid=os.getpid()) is True
        assert (skill_dir / "SKILL.md").read_text() != "user's own skill\n"
        assert backup.exists()
        assert json.loads(marker.read_text())["pids"] == [other.pid]
    finally:
        other.terminate()
        other.wait()
    # Last owner leaves (its pid now dead) -> full teardown restores user skill.
    assert restore_skill(owner_pid=other.pid) is True
    assert (skill_dir / "SKILL.md").read_text() == "user's own skill\n"
    assert not marker.exists()
    assert not backup.exists()
def test_detect_active_repo_cases(project, tmp_path):
    assert detect_active_repo() is None
    repo = tmp_path / "repo"
    inject_skill(repo)  # our own pid is live, so the session counts as active
    assert detect_active_repo() == repo
    _, marker, _ = skill_paths(project)
    record = json.loads(marker.read_text())
    record["pids"] = [dead_pid()]
    marker.write_text(json.dumps(record))
    assert detect_active_repo() is None
    skill_dir, marker, _ = skill_paths(project)
    (skill_dir / "SKILL.md").write_text("live but repo unknown\n")
    marker.write_text(json.dumps({"pids": [os.getpid()], "backup": None}))
    assert detect_active_repo() is None
