"""Case-insensitive substring search over a repo's Markdown notes.

``ugrep`` does the search when it is on PATH; otherwise a built-in Python scan
reads the notes directly. Both paths return the same matches in the same order
(sorted by path, then line), so the fallback is a slower drop-in, not a
different feature. ugrep is optional: nothing breaks without it, but `wrap`
tells the user to install it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

UGREP = "ugrep"
UGREP_INSTALL_HINT = "install it with `sudo apt install ugrep` or `brew install ugrep`"
# ugrep may sit on a slow/large repo; never hang the MCP request forever.
UGREP_TIMEOUT_SECONDS = 30
# One match per 3 output lines: path, line number, whole matching line. Parsing
# by line (not by a ':' separator) keeps paths containing ':' unambiguous.
UGREP_FORMAT = "%f%~%n%~%O%~"


def ugrep_path() -> str | None:
    """Absolute path to the ugrep binary, or None when it is not installed."""
    return shutil.which(UGREP)


def is_note_inside(src: Path, note: Path) -> bool:
    """True when the note entry itself lives under src, wherever it points.

    Containment is judged by the location of the entry, not by the target of a
    symlink: a symlink placed under src/ is a note its owner deliberately
    mounted, even when it points outside src/. Callers must reject '..' parts
    first — a lexical check cannot see through a symlinked dir.
    """
    return src in note.parents


def iter_note_paths(src: Path) -> list[Path]:
    """List *.md notes under src, following symlinks to files and dirs.

    A symlink under src/ is a note (or a dir of notes) even when it points
    outside src/ — that is how an external note is mounted into the repo.
    Hidden notes (a dot-prefixed file or parent dir) are skipped: ugrep does not
    recurse into them either, and both backends must see the same notes. A
    symlink back to an ancestor is skipped, so a loop cannot recurse forever.
    """
    notes: list[Path] = []
    # (dir to visit, realpaths of its ancestors) — a dir whose realpath is
    # already an ancestor closes a loop. Not a global visited set: that would
    # hide the real notes behind a non-looping alias like src/link -> src/sub.
    stack = [(src, frozenset({src.resolve()}))]
    while stack:
        directory, ancestors = stack.pop()
        for entry in directory.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():  # follows symlinked dirs
                real = entry.resolve()
                if real in ancestors:
                    continue
                stack.append((entry, ancestors | {real}))
            elif entry.is_file() and entry.name.endswith(".md"):
                notes.append(entry)
    return sorted(notes)


def search_notes(src: Path, query: str, max_matches: int) -> list[tuple[Path, int, str]]:
    """Return (note, line number, matching line) for each hit, capped at max_matches."""
    ugrep = ugrep_path()
    if ugrep is not None:
        hits = _ugrep_notes(ugrep, src, query, max_matches)
        if hits is not None:
            return hits
    return _scan_notes(src, query, max_matches)


def _ugrep_notes(
    ugrep: str, src: Path, query: str, max_matches: int
) -> list[tuple[Path, int, str]] | None:
    """Search with ugrep; return None when it cannot run, so the caller scans.

    -F keeps the query a literal string (a '.' matches only a '.'), -i matches
    case-insensitively, and -m caps the matches taken from any one note so a
    single big note cannot fill the whole result. No -I: a note whose bytes are
    not valid UTF-8 would count as binary and be skipped, while the scan reads it
    with replacement decoding — hence errors="replace" here, so both backends
    return the same snippet for such a note.
    """
    try:
        result = subprocess.run(
            [
                ugrep,
                "-R",  # recurse, follow symlinks; skips hidden, like iter_note_paths
                "-i",  # case-insensitive
                "-F",  # query is a fixed string, not a regex
                "-s",  # no "permission denied" noise on stderr
                "--include=*.md",
                f"-m{max_matches}",
                f"--format={UGREP_FORMAT}",
                "-e",
                query,
                str(src),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=UGREP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # 0 = matches, 1 = no match, >1 = ugrep failed (fall back to the scan).
    if result.returncode > 1:
        return None
    lines = result.stdout.split("\n")
    hits = []
    for i in range(0, len(lines) - 2, 3):
        path, lineno, line = lines[i], lines[i + 1], lines[i + 2]
        note = Path(path)
        # ugrep only walks below src, so this holds; keep it anyway, so both
        # backends answer to the same boundary rule.
        if not is_note_inside(src, note):
            continue
        hits.append((note, int(lineno), line))
    # ugrep's recursive output order is not path-sorted; match the scan's order.
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    return hits[:max_matches]


def _scan_notes(src: Path, query: str, max_matches: int) -> list[tuple[Path, int, str]]:
    """Fallback: read every note and compare line by line."""
    needle = query.lower()
    hits: list[tuple[Path, int, str]] = []
    for note in iter_note_paths(src):
        content = note.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(content.splitlines(), start=1):
            if needle in line.lower():
                hits.append((note, lineno, line))
                if len(hits) >= max_matches:
                    return hits
    return hits
