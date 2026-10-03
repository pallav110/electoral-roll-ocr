"""roll_year must be read or absent, never asserted.

`app/extractor.py` hardcoded `"roll_year": 2026` into the metadata block it
builds for every extracted roll. Nothing read a year off the PDF. The literal
was stamped onto every row of every roll the system will ever process, and no
field anywhere recorded that it was a guess.

The OCR service's roll_metadata carries no year -- `_extract_roll_header_metadata`
returns state_code, ac_code, anubhag_code, anubhag_name and booth_code only --
so there is currently nothing to read. That makes the correct output `None`:
visibly absent, rather than confidently wrong.

If the OCR service ever starts reporting a year, these tests are the ones that
should notice the app is ignoring it.

The mapping lives inline in `extract()`, which normally performs an HTTP POST to
the OCR service. These tests drive `extract()` with a stubbed httpx response, so
the code under test is the real production path rather than a re-implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _repo_file(name: str) -> Path:
    """Resolve a repo-root file, skipping if this environment does not carry it.

    The compose image bakes in `app/` only -- not ocr_pdf_api.py, not
    pyproject.toml, not package_for_team.py. Tests that read those files are
    real and must run, but they cannot run where the file does not exist, and
    failing there would train everyone to ignore red.
    """
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not present in this checkout (image has app/ only)")
    return path


class _StubResponse:
    """Just enough of httpx.Response for extract()'s .json() call."""

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _extract_common(monkeypatch, roll_metadata: dict) -> dict:
    """Run roll_metadata through the real extract() and return the `common` block."""
    import app.extractor as extractor

    payload = {"ok": True, "roll_metadata": roll_metadata, "records": []}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        return _StubResponse(payload)

    monkeypatch.setattr(extractor.httpx, "post", fake_post)

    # extract() opens the document by path first; point it at this file so the
    # read succeeds and only the OCR call is stubbed.
    result = extractor.extract(
        document_id="00000000-0000-0000-0000-000000000000",
        document_location=str(_repo_file("pyproject.toml")),
        page_from=1,
        page_to=22,
    )
    return result["document_metadata"]["common"]


def test_roll_year_is_null_when_the_ocr_service_reports_no_year(monkeypatch):
    common = _extract_common(monkeypatch, {"ac_code": "042", "anubhag_code": "007"})
    assert common["roll_year"] is None, (
        "roll_year must be NULL when nothing was read off the document. "
        f"Got {common['roll_year']!r}."
    )


def test_roll_year_is_not_a_hardcoded_literal():
    """The specific regression: no year may be asserted as a constant.

    Assigning the literal None is fine -- that is the honest value for "not
    read". What must never reappear is an actual year. This checks the assigned
    value rather than the syntax, so the rule stays "never assert a year"
    instead of "never write a literal".
    """
    source = (REPO_ROOT / "app" / "extractor.py").read_text(encoding="utf-8")
    offenders = []
    for line in source.splitlines():
        if '"roll_year"' not in line or "roll_metadata.get" in line:
            continue
        assigned = line.split('"roll_year":', 1)[1].lstrip()
        # Cut at the first delimiter that ends the value.
        value = assigned.split(",", 1)[0].split("}", 1)[0].strip()
        if value not in ("None", "None}"):
            offenders.append(line.strip())
    assert not offenders, (
        "a year is assigned as a constant again; roll_year must be read from "
        f"roll_metadata or left None: {offenders}"
    )


def test_roll_year_is_used_when_the_ocr_service_eventually_reports_one(monkeypatch):
    """The forward path: if the header gains a year, the app must use it.

    The OCR service does not emit one today, so this passes only by virtue of
    the mapping being generic. It is the contract, not a description of
    current behaviour -- whoever adds the field to _extract_roll_header_metadata
    does not need to touch this file.
    """
    common = _extract_common(monkeypatch, {"ac_code": "042", "roll_year": 2019})
    assert common["roll_year"] == 2019


def test_roll_year_accepts_a_string_year(monkeypatch):
    """The OCR service emits codes as strings; a year would arrive the same way."""
    common = _extract_common(monkeypatch, {"ac_code": "042", "roll_year": "2019"})
    assert common["roll_year"] == 2019


@pytest.mark.parametrize("junk", ["", "  ", None, "not-a-year", "20xx"])
def test_unparseable_year_becomes_null_not_a_crash(monkeypatch, junk):
    """safe_int's contract: anything it cannot read becomes None, not an exception.

    A year that OCRs as "" must not fail the whole roll's metadata mapping.
    """
    common = _extract_common(monkeypatch, {"ac_code": "042", "roll_year": junk})
    assert common["roll_year"] is None


def test_still_reads_the_codes_it_always_did(monkeypatch):
    """Guard against over-correcting: ac_code / anubhag_code must be unaffected."""
    common = _extract_common(monkeypatch, {"ac_code": "042", "anubhag_code": "007"})
    assert common["assembly_constituency_number"] == 42
    assert common["part_number"] == 7


def test_ocr_roll_metadata_does_not_yet_carry_a_year():
    """Documents why roll_year is currently NULL.

    Reads the OCR service's source rather than calling it, so it needs neither
    the container nor cv2. If this ever fails, that is good news: the OCR
    service gained a year, and the forward-path test above now exercises
    production data rather than a stub.
    """
    source = _repo_file("ocr_pdf_api.py").read_text(encoding="utf-8")
    start = source.index("def _extract_roll_header_metadata")
    body = source[start : start + 2500]

    keys = {
        stripped.strip('"')
        for stripped in (line.strip().strip(",") for line in body.splitlines())
        if stripped.endswith("_code") or stripped.endswith("_name")
    }
    assert "roll_year" not in keys, (
        "The OCR service now reports a roll year -- this test is now stale and "
        "the mapping should be verified end to end against a real extraction."
    )