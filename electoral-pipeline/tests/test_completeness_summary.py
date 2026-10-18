from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_pdf_api import _completeness_summary


def test_completeness_summary_counts_missing_and_review_fields():
    records = [
        {"id_card_no": "ABC", "voter_first_name": "राम", "relation_name": "पिता",
         "house_no": "4", "age": "40", "gender": "पुरुष", "needs_review": False},
        {"id_card_no": "", "voter_first_name": "", "relation_name": "",
         "house_no": None, "age": "", "gender": "महिला", "needs_review": True},
    ]
    result = _completeness_summary(records)
    assert result["records"] == 2
    assert result["records_needing_review"] == 1
    assert result["missing_fields"] == {
        "id_card_no": 1, "voter_first_name": 1, "relation_name": 1,
        "house_no": 1, "age": 1, "gender": 0,
    }


def test_empty_summary_is_zeroed():
    result = _completeness_summary([])
    assert result["records"] == 0
    assert result["records_needing_review"] == 0
    assert all(value == 0 for value in result["missing_fields"].values())
