"""Create a shareable source ZIP containing code only -- no voter data.

    python package_for_team.py

WHY THIS IS AN ALLOWLIST
------------------------
The previous version excluded a denylist: {.git, __pycache__, .pytest_cache,
.venv} by directory and {.env, .DS_Store} by name. Its docstring claimed the
result held "no local secrets or runtime state". Simulating that walk over this
repository put 220 files and 86,357,906 bytes in the archive, including:

    input/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf  4,781,968
    OCR/input/2026-EROLLGEN-...-HIN-300-WI.pdf                        4,781,968
    sample-pdfs/2026-EROLLGEN-...-HIN-300-WI.pdf                      4,781,968
    C:tmptest_export.csv                                               379,890
    debug_cards/*.png                                    36 files, cropped cards
    OCR/                                              76 files, incl. 35 of
                                          verified ground-truth voter records

That is the actual electoral roll, three byte-identical copies of it, a CSV
export keyed by EPIC, and 36 images of individual voter cards including their
photo regions -- in a ZIP documented as safe to share. A denylist fails
open: someone adds input_data/ or exports/ and real voter records ship by
default. An allowlist fails closed, so the next PII-bearing directory is
excluded simply by not being on the list.

WHAT SHIPS
----------
Source and configuration only: the app package, tests, scripts, docs, the
compose files and Dockerfiles, and the requirements files the images build
from. Nothing that was derived from a specific roll.

sample-pdfs/ is deliberately excluded even though the README's quickstart
references a PDF there: the only substantive PDF in it is the real roll. Ship a
redacted synthetic sample instead and update the quickstart to match; a real
electoral roll is never shareable, and a zip helper that decides that per-run
based on what happens to be on disk is not a control anyone can rely on.

VERIFYING A CHANGE HERE
-----------------------
    python package_for_team.py --dry-run

prints every path that would be archived, with its size, and asserts the total
stays small. A silent jump in file count means something new got included.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Top-level directories that are code, not data. Anything not listed here and
# not matched by INCLUDE_FILES does not ship.
INCLUDE_DIRS = (
    "app",
    "tests",
    "scripts",
    "DOCS",
)

# Individual files at the repository root that are needed to build or run.
INCLUDE_FILES = (
    "requirements.txt",
    "ocr_api_requirements.txt",
    "dev-requirements.txt",
    "pyproject.toml",
    "package_for_team.py",
    "ocr_pdf_api.py",
    "docker-compose.yml",
    "docker-compose.linux.yml",
    "CLAUDE.md",
    "README.md",
    "TEAM_HANDBOOK.md",
)

INCLUDE_SUFFIXES = (
    ".py",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".md",
    ".sh",
)

# OCR/ is excluded as a directory -- it holds 23 copies of the roll under
# OCR/input/, 35 files of verified ground-truth voter records, and a broken
# test tree. But three genuine source documents live there, so they are
# allowlisted by exact path. Listing them individually keeps the data excluded
# by default while the documentation still reaches the team.
INCLUDE_EXACT = (
    "OCR/CLAUDE.md",
    "OCR/README.md",
    "OCR/ocr_api_requirements.txt",
)

# Never ship these regardless of anything above. .env is handled correctly on
# the other two distribution paths (untracked in git, excluded by
# .dockerignore); this is the third path and it needs its own exclusion.
NEVER_SHIP_NAMES = {".env", ".DS_Store", "Thumbs.db"}
NEVER_SHIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

# A ZIP of source should be well under a megabyte of text. Anything approaching
# that means binary or generated content got in.
MAX_REASONABLE_BYTES = 2_000_000


def _excluded(path: Path, root: Path) -> str | None:
    """Return why `path` is excluded, or None if it ships."""
    rel = path.relative_to(root)
    if path.name in NEVER_SHIP_NAMES:
        return "secret"
    if any(part in NEVER_SHIP_PARTS for part in rel.parts):
        return "cache"
    # Compare on forward slashes so the allowlist is written the same way on
    # Windows and Linux; Path.parts differs between them.
    posix = rel.as_posix()
    if posix in INCLUDE_EXACT:
        return None
    if rel.parts[0] in INCLUDE_DIRS:
        return None
    if rel.name in INCLUDE_FILES:
        return None
    # A loose .txt or .md at the root that is not in INCLUDE_FILES is data
    # (fullpdf.txt, an export dump), not documentation.
    return "not allowlisted"


def collect(root: Path) -> list[Path]:
    """Return every file that ships, sorted for a reproducible archive."""
    selected = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _excluded(path, root) is None:
            selected.append(path)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be archived and exit without writing",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="archive path (default: ../electoral-pipeline-team.zip)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    files = collect(root)
    total = sum(p.stat().st_size for p in files)

    for path in files:
        rel = path.relative_to(root)
        print(f"  {path.stat().st_size:>9,}  {rel}")

    print(f"\n{len(files)} files, {total:,} bytes")

    if args.dry_run:
        return 0

    if total > MAX_REASONABLE_BYTES:
        print(
            f"\nREFUSING to write: {total:,} bytes exceeds "
            f"{MAX_REASONABLE_BYTES:,}. A source archive that large means data "
            f"got included. Re-run with --dry-run and inspect the listing.",
            file=sys.stderr,
        )
        return 1

    from zipfile import ZIP_DEFLATED, ZipFile

    archive = args.output or (root.parent / "electoral-pipeline-team.zip")
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, Path(root.name) / path.relative_to(root))
    print(f"\nwrote {archive} ({archive.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())