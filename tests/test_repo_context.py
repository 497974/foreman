"""Tests for foreman/repo_context.py: build_repo_snapshot.

Purely filesystem-based (no git dependency) — a plain tmp_path directory tree
is enough.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.repo_context import build_repo_snapshot


def _make_tree(root):
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "deep").mkdir()
    (root / "src" / "deep" / "deeper").mkdir()
    (root / "src" / "deep" / "deeper" / "deepest").mkdir()
    (root / "src" / "deep" / "deeper" / "deepest" / "too_deep.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# My Project\n\nShort readme.\n", encoding="utf-8")


def test_snapshot_includes_files_within_max_depth(tmp_path):
    _make_tree(tmp_path)
    snapshot = build_repo_snapshot(tmp_path, max_depth=3)
    assert "src/" in snapshot
    assert "app.py" in snapshot
    assert "deep/" in snapshot
    assert "deeper/" in snapshot


def test_snapshot_excludes_files_beyond_max_depth(tmp_path):
    _make_tree(tmp_path)
    # too_deep.py sits at depth 4 (src/deep/deeper/deepest/too_deep.py) —
    # with max_depth=2 it must not appear at all.
    snapshot = build_repo_snapshot(tmp_path, max_depth=2)
    assert "too_deep.py" not in snapshot


def test_snapshot_excludes_skip_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "some_pkg").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "venv").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "dist").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")

    snapshot = build_repo_snapshot(tmp_path)

    assert ".git" not in snapshot
    assert "node_modules" not in snapshot
    assert "__pycache__" not in snapshot
    assert "venv" not in snapshot.replace("src/", "")  # crude but avoids false positive on unrelated text
    assert "dist" not in snapshot
    assert "build" not in snapshot
    assert "main.py" in snapshot


def test_snapshot_truncates_large_readme(tmp_path):
    (tmp_path / "README.md").write_text("A" * 5000, encoding="utf-8")
    snapshot = build_repo_snapshot(tmp_path, max_preview_chars=100)
    assert "truncated" in snapshot
    # the raw 5000-char blob must not appear in full
    assert "A" * 5000 not in snapshot
    # but a leading slice of it should be present
    assert "A" * 100 in snapshot


def test_snapshot_previews_known_root_files(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")
    snapshot = build_repo_snapshot(tmp_path)
    assert "requirements.txt" in snapshot
    assert "flask==3.0" in snapshot
    assert "package.json" in snapshot
    assert '"name": "demo"' in snapshot


def test_snapshot_handles_empty_directory(tmp_path):
    snapshot = build_repo_snapshot(tmp_path)
    assert "Directory tree" in snapshot
    assert "(empty)" in snapshot
