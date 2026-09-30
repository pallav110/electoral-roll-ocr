"""Versioned extraction contract. Replace only HttpExtractor wiring for your service."""
import logging
from typing import Any

import httpx

from app import config

log = logging.getLogger(__name__)

# The OCR service returns the relation type in Hindi and the relative's name
# under one of four parallel field groups. Map the Hindi type to the group
# that actually holds the name, so a "पति" record does not read a father's
# name that happens to also be present.
_RELATION_FIELDS = {
    "पिता": ("voter_father_first_name", "voter_father_middle_name", "voter_father_last_name"),
    "पति": ("voter_husband_first_name", "voter_husband_middle_name", "voter_husband_last_name"),
    "माता": ("voter_mother_first_name", "voter_mother_middle_name", "voter_mother_last_name"),
    "अन्य": ("voter_other_first_name", "voter_other_middle_name", "voter_other_last_name"),
}

_RELATION_KEY = {
    "पिता": "father",
    "पति": "husband",
    "माता": "mother",
    "अन्य": "other",
}


def _join_name_parts(record: dict, prefix: str) -> str:
    """Re-join OCR's first/middle/last name fields into one display name."""
    parts = [
        str(record.get(f"{prefix}_{p}", "")).strip()
        for p in ("first_name", "middle_name", "sur_name")
    ]
    return " ".join(p for p in parts if p)


def _relation_key(relation_hi) -> str:
    """Hindi relation type -> stable lowercase English key."""
    value = str(relation_hi or "").strip()
    return _RELATION_KEY.get(value, value.lower() or None)


def _join_relation_name(record: dict) -> str:
    """Read the relative's name from the group matching the relation type."""
    fields = _RELATION_FIELDS.get(str(record.get("relation_name", "")).strip())
    if not fields:
        return ""
    parts = [str(record.get(f, "") or "").strip() for f in fields]
    return " ".join(p for p in parts if p)


class ExtractionError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def validate_response(data: Any, page_from: int, page_to: int) -> dict:
    log.info(f"[VALIDATION] Starting response validation for page range {page_from} to {page_to}")
    print(f"[VALIDATION] Starting response validation for page range {page_from} to {page_to}")
    
    if not isinstance(data, dict):
        log.error(f"[VALIDATION] FAILED: Response is not a dictionary, got type {type(data)}")
        print(f"[VALIDATION] FAILED: Response is not a dictionary, got type {type(data)}")
        raise ExtractionError("INVALID_RESPONSE", "Extractor returned a non-object response", False)
    
    log.info(f"[VALIDATION] Response is a valid dictionary, checking success flag")
    print(f"[VALIDATION] Response is a valid dictionary, checking success flag")
    
    if data.get("success") is False:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = str(error.get("code", "EXTRACTION_FAILED"))
        message = str(error.get("message", "Extraction failed"))
        log.error(f"[VALIDATION] FAILED: Extraction service returned success=False with code={code}, message={message}")
        print(f"[VALIDATION] FAILED: Extraction service returned success=False with code={code}, message={message}")
        raise ExtractionError(code, message, code in {"MODEL_TIMEOUT", "EXTRACTION_FAILED", "INTERNAL_ERROR", "EXTRACTION_SERVICE_UNAVAILABLE"})
    
    http_status = data.get("http_status", 200)
    log.info(f"[VALIDATION] HTTP status from response: {http_status}")
    print(f"[VALIDATION] HTTP status from response: {http_status}")
    
    if http_status >= 500:
        log.error(f"[VALIDATION] FAILED: HTTP 5xx error - {http_status}")
        print(f"[VALIDATION] FAILED: HTTP 5xx error - {http_status}")
        raise ExtractionError("EXTRACTION_SERVICE_UNAVAILABLE", f"Extractor returned HTTP {http_status}")
    
    if http_status >= 400:
        log.error(f"[VALIDATION] FAILED: HTTP 4xx error without structured error - {http_status}")
        print(f"[VALIDATION] FAILED: HTTP 4xx error without structured error - {http_status}")
        raise ExtractionError("INVALID_RESPONSE", f"Extractor returned HTTP {http_status} without a structured error", False)
    
    resp_page_from = data.get("page_from")
    resp_page_to = data.get("page_to")
    log.info(f"[VALIDATION] Response page range: {resp_page_from} to {resp_page_to}, expected: {page_from} to {page_to}")
    print(f"[VALIDATION] Response page range: {resp_page_from} to {resp_page_to}, expected: {page_from} to {page_to}")
    
    if resp_page_from != page_from or resp_page_to != page_to:
        log.error(f"[VALIDATION] FAILED: Page range mismatch")
        print(f"[VALIDATION] FAILED: Page range mismatch")
        raise ExtractionError("INVALID_RESPONSE", "Extractor returned a different page range", False)
    
    extractor_version = data.get("extractor_version")
    records = data.get("records")
    log.info(f"[VALIDATION] Extractor version: {extractor_version}, records count: {len(records) if isinstance(records, list) else 'N/A'}")
    print(f"[VALIDATION] Extractor version: {extractor_version}, records count: {len(records) if isinstance(records, list) else 'N/A'}")
    
    if not isinstance(extractor_version, str) or not isinstance(records, list):
        log.error(f"[VALIDATION] FAILED: Missing or invalid extractor_version or records")
        print(f"[VALIDATION] FAILED: Missing or invalid extractor_version or records")
        raise ExtractionError("INVALID_RESPONSE", "Missing extractor_version or records", False)
    
    document_metadata = data.get("document_metadata", {})
    log.info(f"[VALIDATION] Extraction successful, response keys: {list(data.keys()) if isinstance(data, dict) else 'non-dict'}")
    print(f"   ✅ Extraction successful - received {len(data.get('records', []))} voter records")
    
    if not isinstance(document_metadata, dict):
        log.error(f"[VALIDATION] FAILED: Invalid document_metadata type")
        print(f"[VALIDATION] FAILED: Invalid document_metadata type")
        raise ExtractionError("INVALID_RESPONSE", "Invalid document_metadata", False)
    
    log.info(f"[VALIDATION] Document metadata keys: {list(document_metadata.keys()) if isinstance(document_metadata, dict) else 'N/A'}")
    print(f"[VALIDATION] Document metadata keys: {list(document_metadata.keys()) if isinstance(document_metadata, dict) else 'N/A'}")
    
    log.info(f"[VALIDATION] Validating {len(records)} records")
    print(f"[VALIDATION] Validating {len(records)} records")
    
    for idx, item in enumerate(records, 1):
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            log.error(f"[VALIDATION] FAILED: Record {idx} missing source or not a dict")
            print(f"[VALIDATION] FAILED: Record {idx} missing source or not a dict")
            raise ExtractionError("INVALID_RESPONSE", "Record missing source", False)
        
        page = item["source"].get("page_number")
        if not isinstance(page, int) or isinstance(page, bool) or not page_from <= page <= page_to:
            log.error(f"[VALIDATION] FAILED: Record {idx} page {page} outside range {page_from}-{page_to}")
            print(f"[VALIDATION] FAILED: Record {idx} page {page} outside range {page_from}-{page_to}")
            raise ExtractionError("INVALID_RESPONSE", "Record page outside requested range", False)
        
        log.debug(f"[VALIDATION] Record {idx} valid: page={page}, row={item['source'].get('row_number')}")
    
    log.info(f"[VALIDATION] SUCCESS: All {len(records)} records validated")
    print(f"[VALIDATION] SUCCESS: All {len(records)} records validated")
    return data


def mock_extract(request: dict) -> dict:
    """Deterministic shape only; these are synthetic voters, never PDF-derived data."""
    log.info(f"[MOCK_EXTRACT] Starting mock extraction for document_id={request.get('document_id')}")
    print(f"[MOCK_EXTRACT] Starting mock extraction for document_id={request.get('document_id')}")
    
    start, end = request["page_from"], request["page_to"]
    log.info(f"[MOCK_EXTRACT] Page range: {start} to {end}")
    print(f"[MOCK_EXTRACT] Page range: {start} to {end}")
    
    rows = []
    for page in range(start, end + 1):
        for row in range(1, 3):
            serial = (page - 1) * 2 + row
            rows.append({
                "source": {"page_number": page, "row_number": row},
                "hindi": {"name": f"नमूना मतदाता {serial}", "relative_name": "नमूना अभिभावक", "house_number": str(serial), "gender": "पुरुष"},
                "english": {"name": f"Sample Voter {serial}", "relative_name": "Sample Relative", "house_number": str(serial), "gender": "Male"},
                "common": {"serial_number": serial, "epic_number": f"MOCK{serial:07d}", "age": 25, "relationship_type": "father", "section_number": 1},
                "confidence": 0.99,
            })
    
    log.info(f"[MOCK_EXTRACT] Generated {len(rows)} synthetic records")
    print(f"[MOCK_EXTRACT] Generated {len(rows)} synthetic records")
    
    result = {
        "extractor_version": "mock-1.0.0",
        "page_from": start, "page_to": end,
        "document_metadata": {"hindi": {"state": "नमूना राज्य", "district": "नमूना जिला"}, "english": {"state": "Sample State", "district": "Sample District"}, "common": {"roll_year": 2026}},
        "records": rows,
    }
    
    log.info(f"[MOCK_EXTRACT] Mock extraction complete, returning {len(result['records'])} records")
    print(f"[MOCK_EXTRACT] Mock extraction complete, returning {len(result['records'])} records")
    return result


def extract(document_id: str, document_location: str, page_from: int, page_to: int) -> dict:
    log.info(f"[EXTRACT] Starting extraction for document_id={document_id}, location={document_location}, pages={page_from}-{page_to}")
    print(f"   Extracting text from PDF pages {page_from} to {page_to}...")
    
    request = {"document_id": document_id, "document_location": document_location, "page_from": page_from, "page_to": page_to, "schema_version": config.SCHEMA_VERSION}
    log.info(f"[EXTRACT] Extractor mode: {config.EXTRACTOR_MODE}")
    print(f"   Using extraction mode: {config.EXTRACTOR_MODE}")
    
    if config.EXTRACTOR_MODE == "mock":
        log.info(f"[EXTRACT] Using mock extractor")
        print(f"   Using mock data for testing")
        return mock_extract(request)
    
    if config.EXTRACTOR_MODE != "http":
        log.error(f"[EXTRACT] Unknown EXTRACTOR_MODE: {config.EXTRACTOR_MODE}")
        print(f"   ❌ Error: Unknown extraction mode")
        raise ExtractionError("INVALID_CONFIGURATION", f"Unknown EXTRACTOR_MODE: {config.EXTRACTOR_MODE}", False)
    
    # Integration with OCR PDF API service
    # The OCR service expects multipart/form-data with the PDF file and page range
    log.info(f"[EXTRACT] Using HTTP OCR service at: {config.EXTRACTOR_URL}")
    print(f"   Connecting to OCR service...")
    
    headers = {"Authorization": f"Bearer {config.EXTRACTOR_API_KEY}"} if config.EXTRACTOR_API_KEY else {}
    log.info(f"[EXTRACT] Authorization header present: {bool(config.EXTRACTOR_API_KEY)}")
    print(f"   Using authentication: {'Yes' if config.EXTRACTOR_API_KEY else 'No'}")
    
    try:
        # Read PDF file from document_location
        log.info(f"[EXTRACT] Reading PDF file from: {document_location}")
        print(f"   Loading PDF file...")
        
        with open(document_location, "rb") as pdf_file:
            files = {"pdf_file": (document_location.split("/")[-1], pdf_file, "application/pdf")}
            data = {
                "start_page": page_from,
                "end_page": page_to,
                "whole_pdf": False,
                "skip_non_voter_pages": True,
            }
            
            log.info(f"[EXTRACT] Sending request to OCR service with data: {data}")
            print(f"   Sending pages {page_from}-{page_to} to OCR service...")
            
            response = httpx.post(
                config.EXTRACTOR_URL,
                files=files,
                data=data,
                headers=headers,
                timeout=config.EXTRACTOR_TIMEOUT_SECONDS
            )
        
        log.info(f"[EXTRACT] OCR service returned status {response.status_code}")
        print(f"   OCR service responded with status {response.status_code}")
        
        ocr_data = response.json()
        log.info(f"[EXTRACT] OCR response parsed successfully, ok={ocr_data.get('ok')}")
        print(f"[EXTRACT] OCR response parsed successfully, ok={ocr_data.get('ok')}")
        
        # Transform OCR API response to pipeline format
        if not ocr_data.get("ok"):
            error_msg = ocr_data.get("error", "Unknown OCR error")
            log.error(f"[EXTRACT] OCR service returned error: {error_msg}")
            print(f"[EXTRACT] OCR service returned error: {error_msg}")
            raise ExtractionError("EXTRACTION_FAILED", error_msg, False)
        
        # Map OCR response to expected schema
        roll_metadata = ocr_data.get("roll_metadata", {})
        log.info(f"[EXTRACT] Roll metadata keys: {list(roll_metadata.keys())}")
        print(f"[EXTRACT] Roll metadata keys: {list(roll_metadata.keys())}")
        
        # Helper to safely convert string to int
        def safe_int(value):
            # Explicit emptiness check, not truthiness: 0 is falsy, so a
            # `if value` guard turned 0 into None. That silently discarded
            # card_index 0 -- the first card of every page -- which then fell
            # back to the loop counter and filed the record under the wrong row.
            if value is None:
                return None
            if isinstance(value, str) and not value.strip():
                return None
            try:
                return int(value)
            except (ValueError, TypeError):
                return None
        
        transformed = {
            "extractor_version": "ocr-api-v1",
            "page_from": page_from,
            "page_to": page_to,
            "document_metadata": {
                "hindi": {
                    "state": "",
                    "district": "",
                    "assembly_constituency": roll_metadata.get("anubhag_name", ""),
                    "polling_station": "",
                    "polling_station_address": "",
                },
                # Left empty on purpose: English metadata is derived from the
                # Hindi at map time, same as records. Copying anubhag_name here
                # would store Devanagari in an *_en column, which is the exact
                # bug this change exists to remove.
                "english": {
                    "state": "",
                    "district": "",
                    "assembly_constituency": "",
                    "polling_station": "",
                    "polling_station_address": "",
                },
                "common": {
                    "roll_year": 2026,
                    "assembly_constituency_number": safe_int(roll_metadata.get("ac_code")),
                    "part_number": safe_int(roll_metadata.get("anubhag_code")),
                }
            },
            "records": []
        }
        
        log.info(f"[EXTRACT] Transformed metadata structure created")
        print(f"[EXTRACT] Transformed metadata structure created")
        
        # Transform OCR records to pipeline format
        ocr_records = ocr_data.get("records", [])
        log.info(f"[EXTRACT] Transforming {len(ocr_records)} OCR records to pipeline format")
        print(f"[EXTRACT] Transforming {len(ocr_records)} OCR records to pipeline format")
        
        for idx, record in enumerate(ocr_records, 1):
            # Page/row identity. Prefer what the OCR service actually reports.
            #
            # The OCR service tags every record with the page and card index it
            # came from. Ignoring that and reconstructing position arithmetically
            # is wrong the moment a unit covers more than one page: 30 records
            # from page 7 requested as 7-8 get split 15/15, so half are filed
            # under a page they did not come from. Workflow keys identity on
            # (page, row) and rejects duplicates, so this silently mis-attributed
            # rows. With PAGES_PER_UNIT=10 the old arithmetic put every record in
            # a unit on the unit's first page.
            record_page = safe_int(record.get("page_number")) or page_from
            card_index = safe_int(record.get("card_index"))
            # Rows run 1..N in card order within a page. The card index is that
            # same ordering, and unlike idx it does not drift across pages.
            record_row = card_index + 1 if card_index is not None else idx

            # Clamp rather than propagate. A page number outside the requested
            # range makes validate_response raise INVALID_RESPONSE, which
            # discards every other record in the unit over one bad field --
            # the same all-or-nothing failure the safe_int() on age avoids.
            if not page_from <= record_page <= page_to:
                log.warning(
                    "[EXTRACT] Record %d reports page %d outside requested %d-%d; clamping",
                    idx, record_page, page_from, page_to,
                )
                print(f"[EXTRACT] Record {idx}: page {record_page} outside {page_from}-{page_to}, clamping")
                record_page = min(max(record_page, page_from), page_to)
            
            log.debug(f"[EXTRACT] Record {idx}: page={record_page}, row={record_row}, epic={record.get('id_card_no')}")
            print(f"[EXTRACT] Record {idx}: page={record_page}, row={record_row}, epic={record.get('id_card_no')}")

            # Join the parsed name parts back into one display name. The OCR
            # service splits names into first/middle/last because Tesseract
            # reads them from separate label positions; the DB stores both
            # joined and component name fields.
            name_hi = _join_name_parts(record, "voter")
            relative_hi = _join_relation_name(record)

            transformed["records"].append({
                "source": {
                    "page_number": record_page,
                    "row_number": record_row
                },
                "hindi": {
                    "name": name_hi,
                    "relative_name": relative_hi,
                    "house_number": record.get("house_no", ""),
                    "gender": record.get("gender", ""),
                    "section_name": record.get("anubhag_name", ""),
                },
                # Filled by app.transliterate at map time, not here: English
                # must be derived from the same Hindi the record stores, so a
                # later OCR correction regenerates instead of drifting.
                "english": {
                    "name": None,
                    "relative_name": None,
                    "house_number": record.get("house_no", ""),
                    "gender": None,
                    "section_name": None,
                },
                "common": {
                    "serial_number": idx,
                    "epic_number": record.get("id_card_no", ""),
                    # safe_int, not int(): a single OCR misread of the age
                    # ("4S", "" , None) would raise ValueError and abort the
                    # whole unit, discarding every other record in it.
                    "age": safe_int(record.get("age")),
                    "relationship_type": _relation_key(record.get("relation_name")),
                },
                # Raw OCR record fields for component name extraction
                "raw_record": record,
                "confidence": 0.95 if not record.get("needs_review") else 0.70,
            })
        
        log.info(f"[EXTRACT] Transformation complete, returning {len(transformed['records'])} records")
        print(f"[EXTRACT] Transformation complete, returning {len(transformed['records'])} records")
        return transformed
        
    except FileNotFoundError as exc:
        log.error(f"[EXTRACT] PDF file not found: {document_location}")
        print(f"[EXTRACT] PDF file not found: {document_location}")
        raise ExtractionError("INVALID_PDF", f"PDF file not found: {document_location}", False) from exc
    except httpx.TimeoutException as exc:
        log.error(f"[EXTRACT] OCR service timeout after {config.EXTRACTOR_TIMEOUT_SECONDS}s")
        print(f"[EXTRACT] OCR service timeout after {config.EXTRACTOR_TIMEOUT_SECONDS}s")
        raise ExtractionError("EXTRACTION_TIMEOUT", str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        log.error(f"[EXTRACT] OCR service HTTP error: {str(exc)}")
        print(f"[EXTRACT] OCR service HTTP error: {str(exc)}")
        raise ExtractionError("EXTRACTION_SERVICE_UNAVAILABLE", str(exc)) from exc
    except Exception as exc:
        log.error(f"[EXTRACT] Unexpected error during extraction: {str(exc)}")
        print(f"[EXTRACT] Unexpected error during extraction: {str(exc)}")
        raise ExtractionError("INTERNAL_ERROR", f"Unexpected error: {str(exc)}", False) from exc
