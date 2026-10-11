"""Tests for note search. Every test runs on both backends: the real ugrep when
it is installed, and the built-in scan (with ugrep forced off PATH). They must
return identical results, so the fallback stays a drop-in for ugrep."""
from pathlib import Path

import pytest

from recall_engine import search

HAS_UGREP = search.ugrep_path() is not None
@pytest.fixture(params=["ugrep", "scan"])
def backend(request, monkeypatch):
    if request.param == "ugrep":
        if not HAS_UGREP:
            pytest.skip("ugrep not installed")
        return
    monkeypatch.setattr(search, "ugrep_path", lambda: None)
def make_root(base: Path, files: dict[str, str]) -> Path:
    """Create `base` with the given {relative_path: text} files."""
    base.mkdir(parents=True)
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base.resolve()
def test_search_hit_and_miss(backend, tmp_path):
    root = make_root(tmp_path / "root", {"a.md": "intro line\ndeploy keyword here\n"})
    assert search.search_notes([root], "keyword", 50) == [
        (root / "a.md", 2, "deploy keyword here")
    ]
    assert search.search_notes([root], "kubernetes", 50) == []
def test_search_is_case_insensitive(backend, tmp_path):
    # input "abc" must find "AbC" (and "ABC" too).
    root = make_root(tmp_path / "root", {"a.md": "Deploy AbC line\n"})
    assert search.search_notes([root], "abc", 50) == [(root / "a.md", 1, "Deploy AbC line")]
    assert search.search_notes([root], "ABC", 50) == [(root / "a.md", 1, "Deploy AbC line")]
def test_query_is_literal_not_a_regex(backend, tmp_path):
    # "a.b" is a fixed string: the '.' must not match any character.
    root = make_root(tmp_path / "root", {"a.md": "axb\n", "b.md": "a.b\n"})
    assert search.search_notes([root], "a.b", 50) == [(root / "b.md", 1, "a.b")]
def test_search_only_covers_markdown_notes(backend, tmp_path):
    root = make_root(tmp_path / "root", {"a.md": "keyword\n", "b.txt": "keyword\n"})
    assert search.search_notes([root], "keyword", 50) == [(root / "a.md", 1, "keyword")]
def test_matches_are_sorted_by_path_then_line(backend, tmp_path):
    root = make_root(
        tmp_path / "root",
        {
            "b.md": "keyword b\n",
            "a.md": "keyword a1\nfiller\nkeyword a2\n",
            "sub/c.md": "keyword c\n",
        },
    )
    assert search.search_notes([root], "keyword", 50) == [
        (root / "a.md", 1, "keyword a1"),
        (root / "a.md", 3, "keyword a2"),
        (root / "b.md", 1, "keyword b"),
        (root / "sub" / "c.md", 1, "keyword c"),
    ]
def test_multiple_roots_are_merged_and_sorted(backend, tmp_path):
    # Hits from every root come back in one list ordered by absolute path,
    # regardless of the order the roots were passed in.
    root_1 = make_root(tmp_path / "r1", {"z.md": "keyword in r1\n"})
    root_2 = make_root(tmp_path / "r2", {"a.md": "keyword in r2\n"})
    assert search.search_notes([root_2, root_1], "keyword", 50) == [
        (root_1 / "z.md", 1, "keyword in r1"),
        (root_2 / "a.md", 1, "keyword in r2"),
    ]
def test_nested_roots_do_not_duplicate_hits(backend, tmp_path):
    # root/sub is also a root itself: the note is reached through both, but a
    # (path, line) hit is reported only once.
    outer = make_root(tmp_path / "all", {"sub/n.md": "keyword here\n"})
    inner = outer / "sub"
    assert search.search_notes([outer, inner], "keyword", 50) == [
        (inner / "n.md", 1, "keyword here")
    ]
def test_max_matches_caps_results(backend, tmp_path):
    root = make_root(tmp_path / "root", {"a.md": "keyword 1\nkeyword 2\nkeyword 3\n"})
    assert search.search_notes([root], "keyword", 2) == [
        (root / "a.md", 1, "keyword 1"),
        (root / "a.md", 2, "keyword 2"),
    ]
def test_scan_stops_reading_notes_past_the_cap(tmp_path, monkeypatch):
    # The scan must return as soon as the cap is reached: a note after the cap
    # is never read.
    root = make_root(
        tmp_path / "root",
        {"a.md": "keyword 1\nkeyword 2\n", "b.md": "keyword 3\n"},
    )
    monkeypatch.setattr(search, "ugrep_path", lambda: None)
    real_read_text = Path.read_text
    read_notes: list[Path] = []
    def record_read(note, *args, **kwargs):
        read_notes.append(note)
        return real_read_text(note, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", record_read)
    assert search.search_notes([root], "keyword", 2) == [
        (root / "a.md", 1, "keyword 1"),
        (root / "a.md", 2, "keyword 2"),
    ]
    assert root / "b.md" not in read_notes
def test_hidden_notes_are_skipped(backend, tmp_path):
    # ugrep does not recurse into dot-files/dirs, so neither does the scan.
    root = make_root(
        tmp_path / "root",
        {"a.md": "keyword\n", ".hidden.md": "keyword\n", ".arch/b.md": "keyword\n"},
    )
    assert search.search_notes([root], "keyword", 50) == [(root / "a.md", 1, "keyword")]
def test_note_with_invalid_utf8_is_searched(backend, tmp_path):
    # A note that is not valid UTF-8 must still be searched, on both backends,
    # with the bad bytes replaced in the snippet.
    root = make_root(tmp_path / "root", {})
    (root / "a.md").write_bytes("café keyword\n".encode("latin-1"))
    assert search.search_notes([root], "keyword", 50) == [
        (root / "a.md", 1, "caf\ufffd keyword")
    ]
def test_symlinked_note_inside_root_is_searched(backend, tmp_path):
    # A symlink to a note inside the root is itself a note, on both backends.
    root = make_root(tmp_path / "root", {"target.md": "keyword here\n"})
    (root / "link.md").symlink_to("target.md")
    assert search.search_notes([root], "keyword", 50) == [
        (root / "link.md", 1, "keyword here"),
        (root / "target.md", 1, "keyword here"),
    ]
def test_symlink_pointing_outside_root_is_searched(backend, tmp_path):
    # A symlink under a root is a note its owner mounted, wherever it points.
    root = make_root(tmp_path / "root", {"a.md": "keyword inside\n"})
    outside = tmp_path / "outside.md"
    outside.write_text("keyword outside\n")
    (root / "leak.md").symlink_to(outside)
    assert search.search_notes([root], "keyword", 50) == [
        (root / "a.md", 1, "keyword inside"),
        (root / "leak.md", 1, "keyword outside"),
    ]
def test_symlinked_directory_is_followed(backend, tmp_path):
    # A symlinked dir mounts a whole external directory of notes into the root.
    root = make_root(tmp_path / "root", {"a.md": "keyword inside\n"})
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "b.md").write_text("keyword outside\n")
    (root / "linkdir").symlink_to(outside_dir, target_is_directory=True)
    assert search.search_notes([root], "keyword", 50) == [
        (root / "a.md", 1, "keyword inside"),
        (root / "linkdir" / "b.md", 1, "keyword outside"),
    ]
def test_symlink_loop_does_not_recurse_forever(backend, tmp_path):
    # A symlinked dir whose target is an ancestor closes a loop: both backends
    # stop there instead of walking root/loop/loop/... forever.
    root = make_root(tmp_path / "root", {"a.md": "keyword\n", "sub/b.md": "keyword\n"})
    (root / "loop").symlink_to(root, target_is_directory=True)
    (root / "sub" / "loop").symlink_to(root, target_is_directory=True)
    assert search.search_notes([root], "keyword", 50) == [
        (root / "a.md", 1, "keyword"),
        (root / "sub" / "b.md", 1, "keyword"),
    ]
def test_alias_to_a_dir_inside_root_is_searched_by_both_paths(backend, tmp_path):
    # root/alias -> root/sub is an alias, not a loop: ugrep reports the note
    # under both paths, so the scan must not drop one (hence the ancestor-only
    # guard).
    root = make_root(tmp_path / "root", {"sub/b.md": "keyword\n"})
    (root / "alias").symlink_to(root / "sub", target_is_directory=True)
    assert search.search_notes([root], "keyword", 50) == [
        (root / "alias" / "b.md", 1, "keyword"),
        (root / "sub" / "b.md", 1, "keyword"),
    ]
@pytest.mark.skipif(not HAS_UGREP, reason="ugrep not installed")
def test_search_falls_back_to_scan_when_ugrep_cannot_run(tmp_path, monkeypatch):
    # A broken ugrep must not break search: fall back to the scan.
    root = make_root(tmp_path / "root", {"a.md": "keyword here\n"})
    monkeypatch.setattr(search, "ugrep_path", lambda: str(tmp_path / "missing-ugrep"))
    assert search.search_notes([root], "keyword", 50) == [(root / "a.md", 1, "keyword here")]
