"""Shadow occupancy reporting does not alter extraction decisions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_pdf_api as A  # noqa: E402


def test_shadow_report_preserves_fixed_records_and_skip_decision():
    states = [A.CARD_OCCUPANCY_OCCUPIED, A.CARD_OCCUPANCY_BLANK,
              A.CARD_OCCUPANCY_UNCERTAIN]
    records = [{"id": 1}, None, {"id": 2}]
    report = A._occupancy_shadow_report(states, records, fixed_skipped=False)
    assert report["predicted_occupied"] == 1
    assert report["predicted_blank"] == 1
    assert report["predicted_uncertain"] == 1
    assert report["fixed_slots"] == 3
    assert report["record_presence_disagreements"] == 0
    assert report["fixed_skipped"] is False


def test_shadow_report_rejects_slot_length_mismatch():
    import pytest
    with pytest.raises(ValueError, match="length mismatch"):
        A._occupancy_shadow_report(
            [A.CARD_OCCUPANCY_UNCERTAIN], [], fixed_skipped=True
        )


def test_shadow_report_counts_all_slots_when_render_is_missing():
    report = A._occupancy_shadow_report(
        [A.CARD_OCCUPANCY_UNCERTAIN, A.CARD_OCCUPANCY_UNCERTAIN],
        [None, None], fixed_skipped=True,
    )
    assert report["fixed_slots"] == 2
    assert report["predicted_uncertain"] == 2
    assert report["record_presence_disagreements"] == 0
    assert report["fixed_skipped"] is True
