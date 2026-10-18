from types import SimpleNamespace

from app.completeness_export import completeness_rows, completeness_summary


def rec(**kwargs):
    values = dict(
        document_id="doc", page_number=3, card_index=1,
        epic_number="ABC1234567", name_hi="नाम", relative_name_hi="पिता",
        house_number_hi="4", age=40, gender_hi="पुरुष",
        review_reasons=[], is_valid=True,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_completeness_export_reports_missing_fields_without_voter_values():
    rows = completeness_rows([rec(age=None, review_reasons=["age_absent"])])
    assert len(rows) == 1
    assert rows[0]["missing_fields"] == "age"
    assert rows[0]["review_reasons"] == "age_absent"
    assert "नाम" not in rows[0].values()


def test_complete_records_do_not_create_review_rows():
    assert completeness_rows([rec()]) == []
    assert completeness_summary([rec()]) == {
        "records": 1, "review_rows": 0, "missing_fields": {}
    }


def test_review_sheet_contract_is_metadata_only():
    assert [
        "document_id", "page_number", "card_index", "missing_fields",
        "review_reasons", "is_valid",
    ] == [
        "document_id", "page_number", "card_index", "missing_fields",
        "review_reasons", "is_valid",
    ]
