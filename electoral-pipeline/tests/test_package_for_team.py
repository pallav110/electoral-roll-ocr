"""The team ZIP must never contain voter data.

package_for_team.py builds an archive intended to be shared with colleagues. It
used to exclude a denylist, which put 220 files and 86 MB in the archive: the
real electoral roll three times over, a CSV export keyed by EPIC, and 36 cropped
voter card images including their photo regions. Its own docstring claimed the
result held no runtime state.

These tests assert the allowlist holds. They are written against the real
repository rather than a fixture, because the failure mode being guarded is
exactly "someone added a directory containing voter data".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import package_for_team as pkg  # noqa: E402


# Directories that hold real voter records, derived from the roll. If any of
# these ever become allowlisted, these tests fail.
PII_DIRS = {"input", "OCR", "debug_cards", "ingest", "output", "ocr-results", "sample-pdfs"}

PII_SUFFIXES = {".pdf", ".csv", ".xlsx", ".png", ".jpg", ".jpeg"}


@pytest.fixture(scope="module")
def shipped() -> list[Path]:
    return pkg.collect(REPO_ROOT)


def _posix(path: Path) -> str:
    """Repo-relative path with forward slashes, on every platform.

    The allowlist is written posix-style so it reads the same on Windows and
    Linux; Path.parts and str() are not, so assertions have to normalise.
    """
    return path.relative_to(REPO_ROOT).as_posix()


def test_no_voter_data_directory_ships(shipped):
    """Excluded by directory, except exact paths allowlisted for documentation.

    OCR/ holds 23 copies of the roll and 35 files of verified ground-truth
    voter records, but also three real source documents, so it cannot simply be
    excluded wholesale -- package_for_team.INCLUDE_EXACT carries the three.
    """
    offenders = {
        _posix(p) for p in shipped if p.relative_to(REPO_ROOT).parts[0] in PII_DIRS
    } - set(pkg.INCLUDE_EXACT)
    assert not offenders, f"PII directories would be archived: {sorted(offenders)}"


def test_no_binary_or_export_file_ships(shipped):
    offenders = [
        _posix(p) for p in shipped if p.suffix.lower() in PII_SUFFIXES
    ]
    assert not offenders, f"data files would be archived: {offenders}"


def test_no_electoral_roll_pdf_ships(shipped):
    """The specific regression: three byte-identical 4.78 MB copies of the roll."""
    offenders = [
        _posix(p)
        for p in shipped
        if p.suffix.lower() == ".pdf" and "EROLLGEN" in p.name.upper()
    ]
    assert not offenders, f"electoral roll would be archived: {offenders}"


def test_env_file_never_ships(shipped):
    offenders = [_posix(p) for p in shipped if p.name == ".env"]
    assert not offenders, f"secret would be archived: {offenders}"


def test_archive_stays_small(shipped):
    """A source archive is text. Anything approaching a megabyte is data."""
    total = sum(p.stat().st_size for p in shipped)
    assert total < pkg.MAX_REASONABLE_BYTES, (
        f"{len(shipped)} files total {total:,} bytes, over the "
        f"{pkg.MAX_REASONABLE_BYTES:,} ceiling"
    )


def test_the_build_inputs_do_ship(shipped):
    """Guard against over-correcting: without these the ZIP is not buildable."""
    names = {_posix(p) for p in shipped}
    required = {
        "ocr_pdf_api.py",
        "requirements.txt",
        "ocr_api_requirements.txt",
        "docker-compose.yml",
        "pyproject.toml",
    }
    missing = required - names
    assert not missing, f"build inputs missing from archive: {sorted(missing)}"


def test_app_package_ships(shipped):
    names = {_posix(p) for p in shipped}
    assert "app/main.py" in names, "the app package must ship"
    assert "app/workflow.py" in names, "the app package must ship"


def test_ocr_documentation_still_ships(shipped):
    """OCR/ is excluded as a directory, so its 3 real docs need INCLUDE_EXACT.

    Without this, fixing the PII leak would silently drop the OCR service's
    own documentation and requirements from the team archive.
    """
    names = {_posix(p) for p in shipped}
    for expected in pkg.INCLUDE_EXACT:
        assert expected in names, f"{expected} should ship -- it is source, not data"


def test_new_pii_directory_is_excluded_by_default(tmp_path):
    """The whole point of an allowlist: an unlisted directory never ships.

    This is the property the denylist lacked. A new input_data/ directory
    holding voter PDFs is excluded without anyone remembering to add it.
    """
    fake_root = tmp_path / "repo"
    (fake_root / "app").mkdir(parents=True)
    (fake_root / "app" / "main.py").write_text("x", encoding="utf-8")
    (fake_root / "input_data_2026").mkdir()
    (fake_root / "input_data_2026" / "roll.pdf").write_bytes(b"%PDF-1.4")

    collected = pkg.collect(fake_root)
    names = {p.relative_to(fake_root).as_posix() for p in collected}
    assert "app/main.py" in names
    assert "input_data_2026/roll.pdf" not in names


def test_never_ship_names_win_over_allowlist(tmp_path):
    """.env must stay excluded even if someone allowlists it by mistake."""
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / "requirements.txt").write_text("fastapi", encoding="utf-8")
    (fake_root / ".env").write_text("ADMIN_TOKEN=secret", encoding="utf-8")

    collected = pkg.collect(fake_root)
    names = {p.name for p in collected}
    assert "requirements.txt" in names
    assert ".env" not in names