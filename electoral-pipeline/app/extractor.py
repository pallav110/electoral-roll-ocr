"""Versioned extraction contract. Replace only HttpExtractor wiring for your service."""
import logging
from typing import Any

import httpx

from app import config
from app.humanlog import configure_logging

log = logging.getLogger(__name__)
# One writer, plain-English lines. The old code called print() and log.info()
# with identical text, so every line appeared twice in `docker compose logs`.
say = configure_logging("extractor")
log = say.logger


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
    say.info("Checking the reading we got back", pages=f"{page_from}-{page_to}")

    if not isinstance(data, dict):
        say.error("The reading service sent back something that is not a result",
                  pages=f"{page_from}-{page_to}")
        raise ExtractionError("INVALID_RESPONSE", "Extractor returned a non-object response", False)

    if data.get("success") is False:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = str(error.get("code", "EXTRACTION_FAILED"))
        message = str(error.get("message", "Extraction failed"))
        say.error("The reading service could not read these pages",
                  pages=f"{page_from}-{page_to}", reason=message)
        raise ExtractionError(code, message, code in {"MODEL_TIMEOUT", "EXTRACTION_FAILED", "INTERNAL_ERROR", "EXTRACTION_SERVICE_UNAVAILABLE"})

    http_status = data.get("http_status", 200)

    if http_status >= 500:
        say.error("The reading service had a server-side problem",
                  pages=f"{page_from}-{page_to}", status=http_status)
        raise ExtractionError("EXTRACTION_SERVICE_UNAVAILABLE", f"Extractor returned HTTP {http_status}")

    if http_status >= 400:
        say.error("The reading service rejected the request",
                  pages=f"{page_from}-{page_to}", status=http_status)
        raise ExtractionError("INVALID_RESPONSE", f"Extractor returned HTTP {http_status} without a structured error", False)

    resp_page_from = data.get("page_from")
    resp_page_to = data.get("page_to")

    if resp_page_from != page_from or resp_page_to != page_to:
        # This one matters: the pages read are not the pages asked for, so
        # everything downstream would be filed against the wrong page.
        say.error("The reading service read different pages than we asked for",
                  asked=f"{page_from}-{page_to}", got=f"{resp_page_from}-{resp_page_to}")
        raise ExtractionError("INVALID_RESPONSE", "Extractor returned a different page range", False)

    extractor_version = data.get("extractor_version")
    records = data.get("records")

    if not isinstance(extractor_version, str) or not isinstance(records, list):
        say.error("The reading service left out information we need",
                  pages=f"{page_from}-{page_to}")
        raise ExtractionError("INVALID_RESPONSE", "Missing extractor_version or records", False)

    say.info("The reading looks usable", pages=f"{page_from}-{page_to}",
             voters=len(records), version=extractor_version)

    document_metadata = data.get("document_metadata", {})

    if not isinstance(document_metadata, dict):
        say.error("The roll details came back in an unusable form",
                  pages=f"{page_from}-{page_to}")
        raise ExtractionError("INVALID_RESPONSE", "Invalid document_metadata", False)

    for idx, item in enumerate(records, 1):
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            say.error("A voter entry is missing the page it came from",
                      voter=idx, pages=f"{page_from}-{page_to}")
            raise ExtractionError("INVALID_RESPONSE", "Record missing source", False)

        page = item["source"].get("page_number")
        if not isinstance(page, int) or isinstance(page, bool) or not page_from <= page <= page_to:
            # A record filed against a page outside the unit would corrupt the
            # row mapping, so this is a hard stop rather than a warning.
            say.error("A voter was filed against a page we did not read",
                      voter=idx, page=page, pages=f"{page_from}-{page_to}")
            raise ExtractionError("INVALID_RESPONSE", "Record page outside requested range", False)

    say.info("Every voter was checked and looks right", voters=len(records))
    return data


def mock_extract(request: dict) -> dict:
    """Deterministic shape only; these are synthetic voters, never PDF-derived data."""
    say.info("Making up sample voters (no real PDF is read)",
             document=request.get("document_id"))

    start, end = request["page_from"], request["page_to"]

    rows = []
    for page in range(start, end + 1):
        for row in range(1, 3):
            serial = (page - 1) * 2 + row
            rows.append({
                "source": {"page_number": page, "row_number": row},
                "hindi": {"name": f"नमूना मतदाता {serial}", "relative_name": "नमूना अभिभावक", "house_number": str(serial), "gender": "पुरुष"},
                "english": {"name": f"Sample Voter {serial}", "relative_name": "Sample Relative", "house_number": None, "gender": "Male"},
                "common": {"serial_number": serial, "epic_number": f"MOCK{serial:07d}", "age": 25, "relationship_type": "father", "section_number": 1},
                "confidence": 0.99,
            })

    say.info("Sample voters made up", voters=len(rows))

    result = {
        "extractor_version": "mock-1.0.0",
        "page_from": start, "page_to": end,
        "document_metadata": {"hindi": {"state": "नमूना राज्य", "district": "नमूना जिला"}, "english": {"state": "Sample State", "district": "Sample District"}, "common": {"roll_year": 2026}},
        "records": rows,
    }
    
    say.info("Sample reading finished", voters=len(result["records"]))
    return result


def extract(document_id: str, document_location: str, page_from: int, page_to: int) -> dict:
    say.info("Reading a document", pages=f"{page_from}-{page_to}",
             file=document_location.split("/")[-1])

    request = {"document_id": document_id, "document_location": document_location, "page_from": page_from, "page_to": page_to, "schema_version": config.SCHEMA_VERSION}

    if config.EXTRACTOR_MODE == "mock":
        return mock_extract(request)

    if config.EXTRACTOR_MODE != "http":
        say.error("The reading mode is set to something this service does not know",
                  mode=config.EXTRACTOR_MODE)
        raise ExtractionError("INVALID_CONFIGURATION", f"Unknown EXTRACTOR_MODE: {config.EXTRACTOR_MODE}", False)

    # The OCR service expects multipart/form-data with the PDF file and page range.
    headers = {"Authorization": f"Bearer {config.EXTRACTOR_API_KEY}"} if config.EXTRACTOR_API_KEY else {}

    try:
        with open(document_location, "rb") as pdf_file:
            files = {"pdf_file": (document_location.split("/")[-1], pdf_file, "application/pdf")}
            data = {
                "start_page": page_from,
                "end_page": page_to,
                "whole_pdf": False,
                "skip_non_voter_pages": True,
            }

            say.info("Sending the pages to the reader", pages=f"{page_from}-{page_to}")

            response = httpx.post(
                config.EXTRACTOR_URL,
                files=files,
                data=data,
                headers=headers,
                timeout=config.EXTRACTOR_TIMEOUT_SECONDS
            )

        ocr_data = response.json()

        # Transform OCR API response to pipeline format
        if not ocr_data.get("ok"):
            error_msg = ocr_data.get("error", "Unknown OCR error")
            say.error("The reader could not read these pages",
                      pages=f"{page_from}-{page_to}", reason=error_msg)
            raise ExtractionError("EXTRACTION_FAILED", error_msg, False)

        # Map OCR response to expected schema
        roll_metadata = ocr_data.get("roll_metadata", {})

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
        
        # Transform OCR records to pipeline format
        ocr_records = ocr_data.get("records", [])

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
                say.warning(
                    "A voter was filed against a page we did not read, so it was moved to the nearest one",
                    voter=idx, page=record_page, moved_to=min(max(record_page, page_from), page_to))
                record_page = min(max(record_page, page_from), page_to)

            # One line per voter, and only when something is actually running
            # with debug turned on. Thirty cards a page at info level was a
            # wall of noise that hid the lines that mattered.
            log.debug("[extractor] voter=%d page=%d row=%d epic=%s", idx, record_page,
                      record_row, record.get("id_card_no"))

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
                    "house_number": None,
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
                    "anubhag_code": safe_int(record.get("anubhag_code")),
                },
                # Raw OCR record fields for component name extraction
                "raw_record": record,
                "confidence": record.get("confidence"),
            })
        
        say.info("Voters read and put into our format", voters=len(transformed["records"]))
        return transformed

    except FileNotFoundError as exc:
        say.error("The PDF was not where the document said it was",
                  file=document_location)
        raise ExtractionError("INVALID_PDF", f"PDF file not found: {document_location}", False) from exc
    except httpx.TimeoutException as exc:
        say.error("The reader did not answer in time",
                  waited=f"{config.EXTRACTOR_TIMEOUT_SECONDS}s")
        raise ExtractionError("EXTRACTION_TIMEOUT", str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        say.error("Could not talk to the reader", reason=str(exc))
        raise ExtractionError("EXTRACTION_SERVICE_UNAVAILABLE", str(exc)) from exc
    except Exception as exc:
        say.error("Something went wrong while reading", reason=str(exc))
        raise ExtractionError("INTERNAL_ERROR", f"Unexpected error: {str(exc)}", False) from exc
