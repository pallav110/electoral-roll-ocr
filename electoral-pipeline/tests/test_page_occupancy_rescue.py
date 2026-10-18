"""Sparse pages rejected by the fixed EPIC probe must be rescued safely."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_pdf_api as A  # noqa: E402


def test_rescue_accepts_any_occupied_or_uncertain_slot():
    assert A._should_process_page_after_grid_probe(
        [A.CARD_OCCUPANCY_BLANK] * 29 + [A.CARD_OCCUPANCY_OCCUPIED]
    ) is True
    assert A._should_process_page_after_grid_probe(
        [A.CARD_OCCUPANCY_BLANK] * 29 + [A.CARD_OCCUPANCY_UNCERTAIN]
    ) is True


def test_rescue_skips_only_all_confidently_blank_slots():
    assert A._should_process_page_after_grid_probe(
        [A.CARD_OCCUPANCY_BLANK] * 30
    ) is False


def test_rescue_rejects_missing_classifier_results():
    assert A._should_process_page_after_grid_probe([]) is True
    assert A._should_process_page_after_grid_probe([A.CARD_OCCUPANCY_BLANK] * 29) is True
