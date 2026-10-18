"""Cover section list must override a malformed repeated-page code."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_pdf_api as A  # noqa: E402


def test_authoritative_cover_entry_wins_over_page_code_41(monkeypatch):
    """HIN-100 cover says section 1; page OCR incorrectly says 41."""
    cover = {"1": "कन्डुली बडी", "2": "कन्डुली छोटी", "3": "सिमडी"}
    metadata = {
        "anubhag_code": "41",
        "anubhag_name": "कन्डुली बडी",
        "anubhag_entries": {},
        "anubhag_review_reason": "",
    }
    assert A._apply_cover_section_authority(metadata, cover) == {
        **metadata,
        "anubhag_code": "",
        "anubhag_name": "",
        "anubhag_entries": cover,
        "anubhag_status": "per_page",
        "anubhag_review_reason": "",
    }


def test_single_cover_entry_is_copied_to_metadata():
    metadata = {"anubhag_code": "41", "anubhag_name": "wrong"}
    result = A._apply_cover_section_authority(metadata, {"1": "सिमडी"})
    assert result["anubhag_code"] == "1"
    assert result["anubhag_name"] == "सिमडी"
    assert result["anubhag_review_reason"] == ""
