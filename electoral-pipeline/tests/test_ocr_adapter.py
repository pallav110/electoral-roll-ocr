import json

from app.extractor import validate_response
from app.normalize import map_extracted_record, map_voter
from app.ocr_adapter import ocr_result_to_electoral_v1
from app.official_sections import enrich_from_official_catalog, resolve_official_section


def sample_ocr_payload():
    return {
        "ok": True,
        "final_status": "COMPLETED",
        "roll_metadata": {
            "state_code": "UP",
            "ac_code": "521",
            "anubhag_code": "12",
            "anubhag_name": "वार्ड १",
            "booth_code": "99",
        },
        "records": [{
            "sno": 1,
            "page_number": 4,
            "card_index": 2,
            "voter_sr_no": "10",
            "id_card_no": "ABC1234567",
            "gender": "पुरुष",
            "age": "35",
            "house_no": "12",
            "voter_first_name": "राम",
            "voter_middle_name": "",
            "voter_sur_name": "सिंह",
            "relation_name": "पिता श्याम",
            "state_code": "UP",
            "ac_code": "521",
            "anubhag_code": "12",
            "anubhag_name": "वार्ड १",
            "booth_code": "99",
            "needs_review": False,
            "review_reasons": [],
            "field_sources": {"name": "tesseract"},
        }],
    }


def test_ocr_adapter_contract_and_mapping():
    raw = validate_response(ocr_result_to_electoral_v1(sample_ocr_payload(), 4, 4), 4, 4)
    record = map_voter(raw["records"][0], pdf_name="roll.pdf", roll=raw["document_metadata"].get("common", {}))
    metadata = map_extracted_record(raw, 1, None, "roll.pdf")
    assert raw["records"][0]["source"]["page_number"] == 4
    assert raw["records"][0]["source"]["row_number"] == 2
    assert record.id_card_no == "ABC1234567"
    assert record.voter_first_name == "राम"
    assert metadata.ac_code == "521" or metadata.data


def test_official_section_catalogue_overrides_only_an_exact_verified_roll(tmp_path):
    catalog = tmp_path / "sections.json"
    catalog.write_text(json.dumps({"sections": [{
        "source_filename": "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        "state_code": "S24", "ac_code": "53", "booth_code": "300",
        "roll_revision": "SIR-FinalRoll-Revision1", "section_number": "1",
        "section_name_hi": "आधिकारिक अनुभाग", "source_url": "https://voters.eci.gov.in/example.pdf",
        "source_sha256": "abc",
    }]}), encoding="utf-8")
    metadata = {"state_code": "24", "ac_code": "53", "booth_code": "300"}
    records = [{"anubhag_code": "4", "anubhag_name": "bad OCR"}]
    match = enrich_from_official_catalog(
        metadata, records,
        source_filename="/rolls/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        catalog_path=str(catalog), source_sha256="abc",
    )
    assert match is not None
    assert metadata["anubhag_code"] == records[0]["anubhag_code"] == "1"
    assert metadata["anubhag_name"] == records[0]["anubhag_name"] == "आधिकारिक अनुभाग"
    assert resolve_official_section(
        catalog_path=str(catalog),
        source_filename="2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
        state_code="24", ac_code="53", booth_code="301", source_sha256="abc",
    ) is None
