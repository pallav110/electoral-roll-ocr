import uuid

import pytest

from app.extractor import ExtractionError, mock_extract, validate_response
from app.normalize import map_metadata, map_record


def request():
    return {"document_id": str(uuid.uuid4()), "document_location": "/data/pdfs/test.pdf", "page_from": 11, "page_to": 20, "schema_version": "electoral_v1"}


def test_mock_contract_and_bilingual_mapping():
    raw = validate_response(mock_extract(request()), 11, 20)
    assert len(raw["records"]) == 20
    doc_id, session_id, unit_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    record = map_record(raw["records"][0], doc_id, session_id, unit_id)
    metadata = map_metadata(raw, doc_id, session_id)
    assert record.page_number == 11
    assert record.source_row_number == 1
    assert record.name_hi and record.name_en
    assert record.is_valid is True
    assert metadata.state_hi and metadata.state_en


def test_response_rejects_out_of_range_record():
    raw = mock_extract(request())
    raw["records"][0]["source"]["page_number"] = 21
    with pytest.raises(ExtractionError, match="outside requested range"):
        validate_response(raw, 11, 20)


def test_structured_error_classification():
    with pytest.raises(ExtractionError) as exc:
        validate_response({"success": False, "error": {"code": "INVALID_PDF", "message": "broken"}}, 1, 1)
    assert exc.value.code == "INVALID_PDF"
    assert exc.value.retryable is False


def test_invalid_record_is_preserved_with_validation_errors():
    raw = mock_extract(request())
    raw["records"][0]["common"]["age"] = 200
    record = map_record(raw["records"][0], uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert record.is_valid is False
    assert "age outside 0–120" in record.validation_errors
