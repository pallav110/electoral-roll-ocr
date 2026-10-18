from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_pdf_api as A  # noqa: E402


def test_invalid_page_code_resolves_from_cover_name():
    entries = {"1": "ढंगल गांव", "2": "परखण्डई"}
    metadata = {"anubhag_code": "41", "anubhag_name": "ढंगल गांव"}
    resolved = A._resolve_page_section_from_cover(
        metadata, entries, page_code="41", page_name_read="ढंगल गांव"
    )
    assert resolved == ("1", "ढंगल गांव", "")


def test_hin116_ocr_spelling_gav_matches_cover_gaav():
    entries = {"1": "ढंगल गांव", "2": "परखण्डई"}
    resolved = A._resolve_page_section_from_cover(
        {}, entries, page_code="41", page_name_read="ढंगल गाव"
    )
    assert resolved == ("1", "ढंगल गांव", "")


def test_unmatched_invalid_page_code_is_reviewed_not_published():
    entries = {"1": "ढंगल गांव", "2": "परखण्डई"}
    metadata = {"anubhag_code": "41", "anubhag_name": "unknown"}
    resolved = A._resolve_page_section_from_cover(
        metadata, entries, page_code="41", page_name_read="unknown locality"
    )
    assert resolved == ("", "unknown locality", "anubhag_cover_entry_unmatched_for_page")
