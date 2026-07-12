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
def make_src(base: Path, files: dict[str, str]) -> Path:
    """Create <base>/src with the given {relative_path: text} files."""
    src = base / "src"
    src.mkdir(parents=True)
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return src.resolve()
def test_search_hit_and_miss(backend, tmp_path):
    src = make_src(tmp_path, {"a.md": "intro line\ndeploy keyword here\n"})
    assert search.search_notes(src, "keyword", 50) == [
        (src / "a.md", 2, "deploy keyword here")
    ]
    assert search.search_notes(src, "kubernetes", 50) == []
def test_search_is_case_insensitive(backend, tmp_path):
    # input "abc" must find "AbC" (and "ABC" too).
    src = make_src(tmp_path, {"a.md": "Deploy AbC line\n"})
    assert search.search_notes(src, "abc", 50) == [(src / "a.md", 1, "Deploy AbC line")]
    assert search.search_notes(src, "ABC", 50) == [(src / "a.md", 1, "Deploy AbC line")]
def test_query_is_literal_not_a_regex(backend, tmp_path):
    # "a.b" is a fixed string: the '.' must not match any character.
    src = make_src(tmp_path, {"a.md": "axb\n", "b.md": "a.b\n"})
    assert search.search_notes(src, "a.b", 50) == [(src / "b.md", 1, "a.b")]
def test_search_only_covers_markdown_notes(backend, tmp_path):
    src = make_src(tmp_path, {"a.md": "keyword\n", "b.txt": "keyword\n"})
    assert search.search_notes(src, "keyword", 50) == [(src / "a.md", 1, "keyword")]
def test_matches_are_sorted_by_path_then_line(backend, tmp_path):
    src = make_src(
        tmp_path,
        {
            "b.md": "keyword b\n",
            "a.md": "keyword a1\nfiller\nkeyword a2\n",
            "sub/c.md": "keyword c\n",
        },
    )
    assert search.search_notes(src, "keyword", 50) == [
        (src / "a.md", 1, "keyword a1"),
        (src / "a.md", 3, "keyword a2"),
        (src / "b.md", 1, "keyword b"),
        (src / "sub" / "c.md", 1, "keyword c"),
    ]
def test_max_matches_caps_results(backend, tmp_path):
    src = make_src(tmp_path, {"a.md": "keyword 1\nkeyword 2\nkeyword 3\n"})
    assert search.search_notes(src, "keyword", 2) == [
        (src / "a.md", 1, "keyword 1"),
        (src / "a.md", 2, "keyword 2"),
    ]
def test_hidden_notes_are_skipped(backend, tmp_path):
    # ugrep does not recurse into dot-files/dirs, so neither does the scan.
    src = make_src(
        tmp_path,
        {"a.md": "keyword\n", ".hidden.md": "keyword\n", ".arch/b.md": "keyword\n"},
    )
    assert search.search_notes(src, "keyword", 50) == [(src / "a.md", 1, "keyword")]
def test_note_with_invalid_utf8_is_searched(backend, tmp_path):
    # A note that is not valid UTF-8 must still be searched, on both backends,
    # with the bad bytes replaced in the snippet.
    src = make_src(tmp_path, {})
    (src / "a.md").write_bytes("café keyword\n".encode("latin-1"))
    assert search.search_notes(src, "keyword", 50) == [
        (src / "a.md", 1, "caf� keyword")
    ]
def test_symlinked_note_inside_src_is_searched(backend, tmp_path):
    # A symlink to a note inside src/ is itself a note, on both backends.
    src = make_src(tmp_path, {"target.md": "keyword here\n"})
    (src / "link.md").symlink_to("target.md")
    assert search.search_notes(src, "keyword", 50) == [
        (src / "link.md", 1, "keyword here"),
        (src / "target.md", 1, "keyword here"),
    ]
def test_symlink_pointing_outside_src_is_searched(backend, tmp_path):
    # A symlink under src/ is a note its owner mounted, wherever it points.
    src = make_src(tmp_path, {"a.md": "keyword inside\n"})
    outside = tmp_path / "outside.md"
    outside.write_text("keyword outside\n")
    (src / "leak.md").symlink_to(outside)
    assert search.search_notes(src, "keyword", 50) == [
        (src / "a.md", 1, "keyword inside"),
        (src / "leak.md", 1, "keyword outside"),
    ]
def test_symlinked_directory_is_followed(backend, tmp_path):
    # A symlinked dir mounts a whole external directory of notes into src/.
    src = make_src(tmp_path, {"a.md": "keyword inside\n"})
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "b.md").write_text("keyword outside\n")
    (src / "linkdir").symlink_to(outside_dir, target_is_directory=True)
    assert search.search_notes(src, "keyword", 50) == [
        (src / "a.md", 1, "keyword inside"),
        (src / "linkdir" / "b.md", 1, "keyword outside"),
    ]
def test_symlink_loop_does_not_recurse_forever(backend, tmp_path):
    # A symlinked dir whose target is an ancestor closes a loop: both backends
    # stop there instead of walking src/loop/loop/... forever.
    src = make_src(tmp_path, {"a.md": "keyword\n", "sub/b.md": "keyword\n"})
    (src / "loop").symlink_to(src, target_is_directory=True)
    (src / "sub" / "loop").symlink_to(src, target_is_directory=True)
    assert search.search_notes(src, "keyword", 50) == [
        (src / "a.md", 1, "keyword"),
        (src / "sub" / "b.md", 1, "keyword"),
    ]
def test_alias_to_a_dir_inside_src_is_searched_by_both_paths(backend, tmp_path):
    # src/alias -> src/sub is an alias, not a loop: ugrep reports the note under
    # both paths, so the scan must not drop one (hence the ancestor-only guard).
    src = make_src(tmp_path, {"sub/b.md": "keyword\n"})
    (src / "alias").symlink_to(src / "sub", target_is_directory=True)
    assert search.search_notes(src, "keyword", 50) == [
        (src / "alias" / "b.md", 1, "keyword"),
        (src / "sub" / "b.md", 1, "keyword"),
    ]
@pytest.mark.skipif(not HAS_UGREP, reason="ugrep not installed")
def test_search_falls_back_to_scan_when_ugrep_cannot_run(tmp_path, monkeypatch):
    # A broken ugrep must not break search: fall back to the scan.
    src = make_src(tmp_path, {"a.md": "keyword here\n"})
    monkeypatch.setattr(search, "ugrep_path", lambda: str(tmp_path / "missing-ugrep"))
    assert search.search_notes(src, "keyword", 50) == [(src / "a.md", 1, "keyword here")]
