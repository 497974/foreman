"""Builds a textual orientation snapshot of an existing repo for the Planner.

This is deliberately NOT a live source of truth — it is a one-shot, purely
textual briefing the Planner reads once before writing the task list, so tasks
respect existing structure/conventions and don't recreate files that already
serve the same purpose. The executor still uses list_dir/read_file for ground
truth at execution time; nothing here is re-consulted mid-run.

No git dependency: this module works on any directory, git repo or not.
"""

from __future__ import annotations

from pathlib import Path

_SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}

_PREVIEW_FILES = (
    "README.md",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
)


def _walk_tree(root: Path, max_depth: int, max_entries: int) -> list[str]:
    """Render an indented directory tree, depth-limited and entry-capped.

    Depth 0 is the root's direct children. Entries beyond max_entries are
    dropped (with a note) rather than silently truncating mid-directory —
    the Planner should know the listing was cut short, not mistake a partial
    view for the whole repo.
    """
    lines: list[str] = []
    count = 0
    truncated = False

    def walk(dir_path: Path, depth: int, prefix: str) -> None:
        nonlocal count, truncated
        if truncated:
            return
        if depth > max_depth:
            return
        try:
            entries = sorted(
                dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if truncated:
                return
            if entry.name in _SKIP_DIRS:
                continue
            if count >= max_entries:
                truncated = True
                lines.append(f"{prefix}... [truncated, entry limit {max_entries} reached]")
                return
            is_dir = entry.is_dir()
            lines.append(f"{prefix}{entry.name}{'/' if is_dir else ''}")
            count += 1
            if is_dir:
                walk(entry, depth + 1, prefix + "  ")

    walk(root, 0, "")
    return lines


def _preview(path: Path, max_preview_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > max_preview_chars:
        return text[:max_preview_chars] + f"\n... [truncated, {len(text) - max_preview_chars} more chars]"
    return text


def build_repo_snapshot(
    root: Path,
    max_depth: int = 3,
    max_entries: int = 200,
    max_preview_chars: int = 2000,
) -> str:
    """Return a directory tree plus truncated previews of key root-level files.

    Skips .git/node_modules/__pycache__/venv/.venv/dist/build at any depth.
    Purely textual — orients the Planner, is not re-read at execution time.
    """
    root = Path(root)
    sections = ["# Existing project snapshot", "", f"Root: {root}", "", "## Directory tree"]
    tree_lines = _walk_tree(root, max_depth=max_depth, max_entries=max_entries)
    sections.extend(tree_lines if tree_lines else ["(empty)"])

    for name in _PREVIEW_FILES:
        candidate = root / name
        if candidate.is_file():
            preview = _preview(candidate, max_preview_chars)
            sections += ["", f"## {name} (preview)", preview]

    return "\n".join(sections)
