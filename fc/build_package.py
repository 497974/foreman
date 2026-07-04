"""Build the Alibaba Cloud Function Compute (FC) deployment zip for Foreman.

    python fc/build_package.py

Pure stdlib (zipfile only) — no build tooling, no Docker, matches the
project's "runs on nothing but requirements.txt" philosophy for the
orchestration core itself. This script only packages files; it does not
call any Alibaba Cloud API and needs no credentials.

What goes in the zip (fc/foreman-fc.zip), relative to the repo root:
    foreman/            - the package (planner/executor/verifier/etc.)
    serve.py            - the web console entry point (has --host/--port)
    main.py             - CLI entry point (not used by FC, harmless to ship)
    requirements.txt    - documentation of deps; FC custom runtime does NOT
                          auto-pip-install this (see docs/DEPLOY.md)
    demo/               - sample checklists or referenced in READMEs
    fc/bootstrap        - the custom-runtime entrypoint FC actually execs
    fc/vendor/          - OPTIONAL: vendored pip deps (see below), included
                          only if the directory exists at build time

What is deliberately EXCLUDED (never shipped):
    runs/       - per-run ledger/workspace state; ephemeral on FC anyway
    evals/      - evaluation harness output, not needed to serve the console
    .env        - THE API KEY LIVES HERE. Never zip it. Set DASHSCOPE_API_KEY
                  etc. as FC *environment variables* in the console instead
                  (see docs/DEPLOY.md) — this is the whole reason build_package
                  is a separate reviewable step instead of `zip -r`.
    tests/      - test suite, not needed at runtime
    .git/       - repo metadata, not needed at runtime and can be large

Two ways to get the `openai` package (Foreman's only runtime dependency)
onto FC; both are documented in docs/DEPLOY.md:

  Option 1 (console-side): after uploading the zip, use the FC console's
  layer / "install dependencies" step to pip-install requirements.txt inside
  the function's environment. No change needed here.

  Option 2 (vendored, recommended for a beginner — no extra console step,
  one less thing that can silently fail): run

      pip install openai -t fc/vendor

  BEFORE running this script. If fc/vendor exists, build_package.py includes
  it in the zip under fc/vendor/, and fc/bootstrap prepends it to
  PYTHONPATH at boot so `import openai` resolves with no console-side step.
"""

from __future__ import annotations

import stat
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FC_DIR = REPO_ROOT / "fc"
OUTPUT_ZIP = FC_DIR / "foreman-fc.zip"

# Top-level items to include, relative to REPO_ROOT. Directories are walked
# recursively (with per-file exclusion rules applied below); files are
# included as-is.
INCLUDE_DIRS = ["foreman", "demo"]
INCLUDE_FILES = ["serve.py", "main.py", "requirements.txt"]

# fc/bootstrap is always included; fc/vendor/ is included only if present
# (see module docstring, Option 2). Nothing else under fc/ (e.g. this build
# script, the output zip itself, __pycache__) should ever be shipped.
BOOTSTRAP_REL = "fc/bootstrap"
VENDOR_DIR = FC_DIR / "vendor"

# Never include these, no matter where they appear in a walked directory.
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".git", "runs", "evals", "tests"}
EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_FILE_NAMES = {".env", ".DS_Store"}


def _should_skip_dir(dirname: str) -> bool:
    return dirname in EXCLUDE_DIR_NAMES


def _should_skip_file(path: Path) -> bool:
    if path.name in EXCLUDE_FILE_NAMES:
        return True
    if path.suffix in EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def _iter_dir_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(_should_skip_dir(part) for part in path.relative_to(REPO_ROOT).parts):
            continue
        if _should_skip_file(path):
            continue
        yield path


def build(output_zip: Path = OUTPUT_ZIP) -> Path:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    added = 0
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Top-level standalone files (serve.py, main.py, requirements.txt).
        for rel in INCLUDE_FILES:
            src = REPO_ROOT / rel
            if not src.exists():
                raise FileNotFoundError(f"expected file missing: {src}")
            zf.write(src, arcname=rel)
            added += 1

        # 2. Directories, walked recursively with exclusions applied.
        for rel_dir in INCLUDE_DIRS:
            src_dir = REPO_ROOT / rel_dir
            if not src_dir.is_dir():
                raise FileNotFoundError(f"expected directory missing: {src_dir}")
            for path in _iter_dir_files(src_dir):
                arcname = path.relative_to(REPO_ROOT).as_posix()
                zf.write(path, arcname=arcname)
                added += 1

        # 3. fc/bootstrap — the FC custom-runtime entrypoint. Set the unix
        # executable bit in the zip's external_attr so it doesn't need a
        # manual `chmod +x` after FC unzips it (zipfile defaults to 0644 for
        # files added via write(), which is not executable).
        bootstrap_src = REPO_ROOT / BOOTSTRAP_REL
        if not bootstrap_src.exists():
            raise FileNotFoundError(f"expected file missing: {bootstrap_src}")
        info = zipfile.ZipInfo(BOOTSTRAP_REL)
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with open(bootstrap_src, "rb") as f:
            zf.writestr(info, f.read())
        added += 1

        # 4. fc/vendor/ — optional vendored pip deps (Option 2). Included
        # only if the directory exists (i.e. the user ran
        # `pip install openai -t fc/vendor` first per docs/DEPLOY.md).
        if VENDOR_DIR.is_dir():
            for path in _iter_dir_files(VENDOR_DIR):
                arcname = path.relative_to(REPO_ROOT).as_posix()
                zf.write(path, arcname=arcname)
                added += 1

    return output_zip, added


def main() -> int:
    vendor_present = VENDOR_DIR.is_dir()
    output_zip, added = build()
    size_kb = output_zip.stat().st_size / 1024
    print(f"Wrote {output_zip} ({size_kb:.1f} KiB, {added} files)")
    if vendor_present:
        print("Included fc/vendor/ (vendored pip deps) — PYTHONPATH option 2 is active.")
    else:
        print(
            "fc/vendor/ not found — the zip does NOT include openai. Either:\n"
            "  (1) use the FC console's layer/pip-install step after upload, or\n"
            "  (2) run `pip install openai -t fc/vendor` and re-run this script.\n"
            "See docs/DEPLOY.md for both options."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
