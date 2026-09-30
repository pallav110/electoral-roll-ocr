"""Page attribution must follow the OCR service, not arithmetic.

The OCR service tags every record with the page and card index it was cut
from. Before this was honoured, app/extractor.py reconstructed position from
the loop counter, which silently filed records under the wrong page for any
unit covering more than one page.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import config
from app.extractor import ExtractionError, _join_name_parts, _join_relation_name, extract, validate_response


def _ocr_record(idx, page, card, include_provenance=True):
    rec = {
        "sno": str(idx),
        "id_card_no": f"ABC{page:03d}{card:04d}",
        "gender": "पुरुष",
        "age": "45",
        "house_no": "123",
        "voter_first_name": "राजीव",
        "voter_middle_name": "",
        "voter_sur_name": "सक्सेना",
        "relation_name": "पिता",
        "voter_father_first_name": "शिव",
    }
    if include_provenance:
        rec["page_number"] = page
        rec["card_index"] = card
    return rec


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def fake_post(monkeypatch, tmp_path):
    """Patch httpx.post and point the PDF path at a real (empty) file."""
    pdf = tmp_path / "roll.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    captured = {}

    def _post(url, files, data, headers, timeout):
        captured["data"] = data
        start, end = int(data["start_page"]), int(data["end_page"])
        records = []
        for page in range(start, end + 1):
            # card_index restarts at 0 for every page, matching the real
            # service: page_records is built fresh inside the per-page loop.
            for card in range(30):
                records.append(_ocr_record(len(records) + 1, page, card))
        captured["payload"] = {
            "ok": True,
            "roll_metadata": {"anubhag_name": "किशननगर", "ac_code": "53", "anubhag_code": "24"},
            "records": records,
        }
        return _Resp(captured["payload"])

    monkeypatch.setattr("app.extractor.httpx.post", _post)
    monkeypatch.setattr(config, "EXTRACTOR_MODE", "http")
    monkeypatch.setattr(config, "EXTRACTOR_TIMEOUT_SECONDS", 60)
    captured["path"] = str(pdf)
    return captured


def test_records_keep_their_own_page_across_a_multi_page_unit(fake_post):
    """A 3-page unit must yield 30 rows on each of the 3 pages, not 90 on page 1."""
    out = extract("doc", fake_post["path"], page_from=5, page_to=7)

    by_page = {}
    for rec in out["records"]:
        by_page.setdefault(rec["source"]["page_number"], []).append(rec["source"]["row_number"])

    assert sorted(by_page) == [5, 6, 7]
    for page, rows in by_page.items():
        assert len(rows) == 30, f"page {page} got {len(rows)} rows"
        assert sorted(rows) == list(range(1, 31)), f"page {page} rows not 1..30: {rows}"


def test_response_passes_validation_for_a_multi_page_unit(fake_post):
    """The mis-attributed version put every record on page 5 and still validated;
    this asserts the fix does not break the range check that catches regressions."""
    out = extract("doc", fake_post["path"], page_from=5, page_to=7)
    validate_response(out, 5, 7)


def test_falls_back_to_requested_page_when_service_omits_provenance(fake_post, monkeypatch):
    """An older OCR service without page_number must still work, not crash."""
    def _post(url, files, data, headers, timeout):
        return _Resp({
            "ok": True,
            "roll_metadata": {},
            "records": [
                _ocr_record(1, 0, 0, include_provenance=False),
                _ocr_record(2, 0, 1, include_provenance=False),
            ],
        })

    monkeypatch.setattr("app.extractor.httpx.post", _post)
    out = extract("doc", fake_post["path"], page_from=9, page_to=9)
    assert {r["source"]["page_number"] for r in out["records"]} == {9}


def test_page_outside_requested_range_is_clamped_not_fatal(fake_post, monkeypatch):
    """One bad page number must not abort the whole unit.

    validate_response rejects any record outside the requested range, so an
    unclamped value would raise INVALID_RESPONSE and throw away all 30 records
    in the unit over one field.
    """
    records = [_ocr_record(i, 5, i) for i in range(30)]
    records[7]["page_number"] = 99  # one bad field among 30 good records

    def _post(url, files, data, headers, timeout):
        return _Resp({"ok": True, "roll_metadata": {}, "records": records})

    monkeypatch.setattr("app.extractor.httpx.post", _post)
    out = extract("doc", fake_post["path"], page_from=5, page_to=5)

    assert len(out["records"]) == 30, "a single bad page number must not drop the unit"
    assert {r["source"]["page_number"] for r in out["records"]} == {5}
    validate_response(out, 5, 5)  # must not raise


def test_card_index_zero_is_not_treated_as_missing():
    """Regression: safe_int returned None for 0, discarding the first card.

    card_index 0 is the first card of every page, so this is not a rare edge
    case -- it silently mis-filed one record per page.
    """
    def _post(url, files, data, headers, timeout):
        return _Resp({
            "ok": True,
            "roll_metadata": {},
            "records": [_ocr_record(1, 4, 0), _ocr_record(2, 4, 1)],
        })

    with patch("app.extractor.httpx.post", _post):
        out = extract("doc", "/dev/null", page_from=4, page_to=4)
    assert [r["source"]["row_number"] for r in out["records"]] == [1, 2]


def test_relation_name_reads_the_group_matching_the_relation_type():
    """A 'पति' record must not read a father's name that is also present."""
    record = {
        "relation_name": "पति",
        "voter_father_first_name": "शिव",
        "voter_husband_first_name": "सुनीता",
        "voter_husband_middle_name": "कुमार",
        "voter_husband_last_name": "वर्मा",
    }
    assert _join_relation_name(record) == "सुनीता कुमार वर्मा"


def test_name_parts_are_rejoined_in_order():
    record = {"voter_first_name": "राजीव", "voter_middle_name": "कुमार", "voter_sur_name": "सक्सेना"}
    assert _join_name_parts(record, "voter") == "राजीव कुमार सक्सेना"
