from __future__ import annotations
import json
from app.official_sections import enrich_from_official_catalog, resolve_official_section


def _catalog(tmp_path):
    path = tmp_path / "sections.json"
    path.write_text(json.dumps({"sections": [{
        "source_filename": "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        "state_code": "S24", "ac_code": "53", "booth_code": "300",
        "roll_revision": "SIR-FinalRoll-Revision1", "section_number": "1",
        "section_name_hi": "आधिकारिक अनुभाग", "source_url": "https://voters.eci.gov.in/example.pdf",
        "source_sha256": "abc"}]}), encoding="utf-8")
    return path


def test_exact_match_enriches_only_the_same_official_roll(tmp_path):
    metadata = {"state_code": "24", "ac_code": "53", "booth_code": "300"}
    rows = [{"anubhag_code": "4", "anubhag_name": "bad OCR"}]
    found = enrich_from_official_catalog(metadata, rows,
        source_filename="/x/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        catalog_path=str(_catalog(tmp_path)), source_sha256="abc")
    assert found and metadata["anubhag_code"] == rows[0]["anubhag_code"] == "1"
    assert metadata["anubhag_name"] == rows[0]["anubhag_name"] == "आधिकारिक अनुभाग"
    assert metadata["anubhag_status"] == rows[0]["anubhag_status"] == "official_verified"
    assert rows[0]["field_sources"]["anubhag_metadata"]["kind"] == "official_section_catalog"


def test_mismatch_never_overwrites_ocr(tmp_path):
    path = _catalog(tmp_path)
    assert resolve_official_section(catalog_path=str(path),
        source_filename="2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        state_code="24", ac_code="53", booth_code="301", source_sha256="abc") is None
    metadata, rows = {"state_code": "24", "ac_code": "53", "booth_code": "300"}, [{"anubhag_name": "OCR"}]
    assert enrich_from_official_catalog(metadata, rows,
        source_filename="2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        catalog_path=str(path), source_sha256="wrong") is None
    assert rows[0]["anubhag_name"] == "OCR"
