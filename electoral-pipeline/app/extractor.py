"""Versioned extraction contract. Replace only HttpExtractor wiring for your service."""
import logging
import re
import unicodedata
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


# The spacing matras, excluded from a skeleton. Copied verbatim from
# ocr_text_pdf_api._SPACING_MATRAS; the two lists are asserted equal by
# test_ocr_routing, so neither can drift without the tests failing.
#
# They are listed even though the combining-class test below does not remove
# them: every Devanagari matra carries Unicode combining class 0, including
# U+093E, so there is no class-based way to exclude one. The list is an
# explicit denylist and the reason it exists is that nothing general covers
# it.
_SPACING_MATRAS = "ऺऻ़ऽॕॖॗॢॣॺॻॼॽ"

# Punctuation that separates a label from its value, dropped from a skeleton so
# "नाम : 28" and "नाम: 28" reduce alike. Copied from
# ocr_text_pdf_api._SEPARATORS; pinned by test_ocr_routing.
_SKELETON_SEPARATORS = ":;" + "।॥,/-"

# This template's ward-number label as the text layer stores it, in both of the
# fonts the sample PDF embeds the header in. Duplicated from
# ocr_text_pdf_api._LBL_WARD / _LBL_WARD_ALT rather than imported: the web app
# does not depend on the OCR image, and importing it would drag PyMuPDF,
# Tesseract bindings and the sibling service into every request path.
_WARD_LABELS = ("वपरर सनखयप", "रररर ससखजर")


def _mentions_ward_header(line: str) -> bool:
    """True if a stored header line is this template's ward-number label.

    The stored spelling has the matras stripped ("वपरर सनखयप" for "वार्ड
    संख्या"), and the same document embeds two different corruptions on
    different pages, so both are listed. Only the consonant skeleton is
    compared, which is what survives either encoding intact.

    A number on the same line is required. The label on its own is not
    evidence -- this template's own cover page lists the form's field names
    ("वार्ड क्रम संख्या", "मतदान केन्द्र की संख्या") with no value beside
    them, and matching those would classify the cover as a header page.
    """
    if not re.search(r"\d", line):
        return False
    skeleton = _deva_skeleton(line)
    if not skeleton:
        return False
    return any(skeleton.startswith(_deva_skeleton(label))
               for label in _WARD_LABELS)


def _deva_skeleton(text: str) -> str:
    """The consonants of a Devanagari string, ignoring matras and spacing.

    Storing a Devanagari string destroys the matras, so two spellings of one
    word differ by whole characters and no edit-distance threshold separates
    them. Their consonants are identical, which is what makes matching on the
    skeleton reliable across encodings.

    The spacing matras are excluded by an explicit list, not by combining
    class: every Devanagari matra is class 0, so no class-based test can
    remove one. Punctuation is dropped too, so "नाम : 28" and "नाम: 28"
    produce the same skeleton.

    Note what this does NOT do: it does not normalise "संख्या" to "सनखयप".
    Those are different character sequences, not one matra-stripped form of
    the other, and no amount of stripping relates them -- which is why the
    two header encodings are matched as separate exact variants.

    The rule is kept identical to ocr_text_pdf_api._is_skeleton_char, and
    test_ocr_routing asserts the two agree character for character on real
    header lines. Divergence here would be silent: the router and the service
    would disagree about which pages carry a header, and the symptom is a roll
    that quietly loses its metadata rather than an error.
    """
    out = []
    for ch in unicodedata.normalize("NFC", text):
        if (unicodedata.combining(ch) or ch in _SPACING_MATRAS
                or ch.isspace() or ch in _SKELETON_SEPARATORS):
            continue
        if "ऀ" <= ch <= "ॿ":
            out.append(ch)
    return "".join(out)


def _resolve_ocr_url(document_location: str) -> tuple[str, str, list]:
    """Return (url, kind, boxes) for this PDF.

    The gate in roll_gate decides before either reader is contacted:

      scanned           image-only and on the booth grid → the image reader
      scanned-labels    image-only, voter IDs off that grid → the label reader
      text-ward         the Jaipur ward header → the text reader
      not-a-roll        text, but no voter cards → refused, no URL
      new-card-format   a text layer with voter IDs the ward reader does not
                        own → the label reader
      unclassified      the file could not be read → the configured default

    An empty URL is a refusal. The caller raises from ``kind`` so a kundli
    and a new card format do not share one sentence.
    """
    default_url = config.SCANNED_EXTRACTOR_URL
    if not config.OCR_ROUTER:
        return default_url, "default", []

    from app.roll_gate import classify

    name = document_location.rsplit("/", 1)[-1]
    gate = classify(document_location)

    if gate.kind == "scanned":
        say.info(
            "This PDF has no text layer, so it is an image-scanned roll",
            file=name, service="scanned-roll OCR")
        return default_url, "scanned", []

    if gate.kind == "scanned-labels":
        say.info(
            "This scanned PDF has voter cards off the booth grid, so the "
            "label reader will try it",
            file=name, cards=len(gate.boxes))
        return "", "scanned-labels", gate.boxes

    if gate.kind == "text-ward":
        say.info(
            "Born-digital ward-format roll: it has a text layer and the "
            "municipal ward header, so the text-layer reader can measure "
            "its card grid",
            file=name, service="text-layer OCR", format="ward")
        return config.TEXT_EXTRACTOR_URL, "text-ward", []

    if gate.kind == "not-a-roll":
        say.error(
            "This PDF has a text layer but no voter cards, so it was not read",
            file=name)
        return "", "not-a-roll", []

    if gate.kind == "new-card-format":
        say.info(
            "This PDF has voter cards in a layout the measured readers do not "
            "know, so the label reader will try it",
            file=name, cards=len(gate.boxes))
        return "", "new-card-format", gate.boxes

    say.warning(
        "Could not classify this PDF, so it is going to the default OCR "
        "service",
        file=name)
    return default_url, "unclassified", []


def _detail_of(payload: Any) -> str:
    """Pull the human-readable message out of a JSON error body.

    FastAPI reports its own errors as {"detail": ...}, where the detail is a
    string for a raised HTTPException but a list of field objects for a
    request-validation failure. Both shapes are reduced to text here so the
    log carries something a person can act on.
    """
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                parts.append(str(item.get("msg") or item.get("detail") or item))
            else:
                parts.append(str(item))
        return "; ".join(p for p in parts if p)
    return ""


def _response_error(response: Any) -> str:
    """Best available message from a failed HTTP response."""
    try:
        payload = response.json()
    except Exception:
        text = (getattr(response, "text", "") or "").strip()
        return text[:500] or f"HTTP {response.status_code}"
    return _detail_of(payload) or str(payload)[:500] or f"HTTP {response.status_code}"


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
        # roll_year is None for the same reason as in extract(): nothing read a year off
        # a document. mock_extract reads no document at all, so a plausible-looking
        # 2026 here would only lend the mock's fabricated data an air of realism it
        # has no claim to -- and would leave this literal as the last place the
        # hardcoded year survives, where it would be indistinguishable from the
        # real bug if this line were ever copied.
        "document_metadata": {"hindi": {"state": "नमूना राज्य", "district": "नमूना जिला"}, "english": {"state": "Sample State", "district": "Sample District"}, "common": {"roll_year": None}},
        "records": rows,
    }
    
    say.info("Sample reading finished", voters=len(result["records"]))
    return result


def _save_layout_sample(adapted, route_kind: str) -> None:
    """Store the measured offsets. A failure here does not fail the read."""
    try:
        from app.roll_template import save_unconfirmed

        path = save_unconfirmed(adapted, route_kind)
    except Exception as exc:
        say.warning("Could not save the layout sample", reason=str(exc))
        return
    if path is not None:
        say.info("Saved an unconfirmed layout sample", file=path.name)


def _refusal(kind: str, boxes: list) -> ExtractionError:
    """The error a refused file raises. Neither kind is retried."""
    from app.roll_gate import format_box_list

    if kind == "not-a-roll":
        return ExtractionError(
            "NOT_A_ROLL",
            "This file has a text layer but no voter cards, so it was not read.",
            False,
        )
    if kind in ("new-card-format", "scanned-labels"):
        count = len(boxes)
        noun = "card" if count == 1 else "cards"
        return ExtractionError(
            "NEW_CARD_FORMAT",
            f"This file has {count} {noun}, but they do not print a name label "
            "the reader can follow, so nothing was extracted. "
            f"Boxes: {format_box_list(boxes)}",
            False,
        )
    return ExtractionError(
        "UNSUPPORTED_ROLL_FORMAT",
        "No OCR reader handles this file, so it was not read.",
        False,
    )


def _post_ocr(document_location, ocr_url, route_kind, page_from, page_to, headers):
    """Send one page range to the reader the gate chose."""
    with open(document_location, "rb") as pdf_file:
        files = {"pdf_file": (document_location.split("/")[-1], pdf_file, "application/pdf")}
        data = {
            "start_page": page_from,
            "end_page": page_to,
            "whole_pdf": False,
            "skip_non_voter_pages": True,
        }
        say.info("Sending the pages to the reader", pages=f"{page_from}-{page_to}",
                 reader=route_kind)
        response = httpx.post(
            ocr_url,
            files=files,
            data=data,
            headers=headers,
            timeout=config.EXTRACTOR_TIMEOUT_SECONDS,
        )

    # Read the status before the body. Both OCR services answer a template
    # they cannot read with 422 and a message naming the other service, and
    # that message is the whole diagnosis -- but it arrives as {"detail": ...},
    # with neither "ok" nor "error" in it. Falling through to the generic
    # check below reported every one of those as "Unknown OCR error", throwing
    # away the only useful sentence in the response.
    #
    # getattr with a 200 default, not response.status_code: several tests
    # substitute a minimal stand-in that implements only .json(). A real
    # httpx.Response always carries the attribute, so the default is
    # unreachable in production and the check simply does not fire for those
    # objects, which is the correct reading -- a body was returned with no
    # error status to indicate otherwise.
    if getattr(response, "status_code", 200) >= 400:
        error_msg = _response_error(response)
        say.error("The reader rejected these pages",
                  pages=f"{page_from}-{page_to}",
                  status=response.status_code, reason=error_msg)
        # Not retryable: the same pages will fail the same way, and the
        # message names what to do instead.
        raise ExtractionError("EXTRACTION_FAILED", error_msg, False)
    return response.json()


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
        ocr_url, route_kind, boxes = _resolve_ocr_url(document_location)
        adapted = None
        if not ocr_url and route_kind in ("new-card-format", "scanned-labels"):
            # The measured readers stay out of this file. The adapter reads
            # the labels inside the card boxes. A cover page has no cards and
            # comes back empty; a card with no labels is still a refusal.
            from app.ocr_adapter import read_pages

            adapted = read_pages(document_location, page_from, page_to)
            if adapted.status == "unlabeled":
                raise _refusal(route_kind, boxes)
            say.info(
                "Reading this roll from the labels printed on its cards",
                pages=f"{page_from}-{page_to}", voters=len(adapted.records))
            if adapted.records:
                _save_layout_sample(adapted, route_kind)
        elif not ocr_url:
            # The router refused this file, and returned no URL precisely so
            # that nothing is sent. Raising here is what makes the refusal
            # visible: without it the request would go nowhere and surface as
            # a connection error, which reads as "the reader is down" and
            # sends an operator looking at the wrong service.
            #
            # Not retryable. The format of a file does not change between
            # attempts, so a retry loop would burn every attempt and then
            # report a timeout instead of the real reason.
            raise _refusal(route_kind, boxes)

        if adapted is not None:
            ocr_data = {"ok": True, "roll_metadata": {}, "records": adapted.records}
        else:
            ocr_data = _post_ocr(
                document_location, ocr_url, route_kind, page_from, page_to, headers)

        if not ocr_data.get("ok"):
            error_msg = ocr_data.get("error") or _detail_of(ocr_data) or "Unknown OCR error"
            say.error("The reader could not read these pages",
                      pages=f"{page_from}-{page_to}", reason=error_msg)
            raise ExtractionError("EXTRACTION_FAILED", error_msg, False)

        # Map OCR response to expected schema
        roll_metadata = ocr_data.get("roll_metadata", {})
        if config.ECI_SOURCE_VERIFY_ENABLED:
            from app.eci_source import verify_uploaded_roll
            source_verification = verify_uploaded_roll(document_location)
            roll_metadata["official_source"] = source_verification
            for ocr_record in ocr_data.get("records", []):
                sources = dict(ocr_record.get("field_sources") or {})
                sources["roll_metadata"] = source_verification
                ocr_record["field_sources"] = sources
            say.info("Official ECI source verification finished",
                     file=document_location.split("/")[-1],
                     status=source_verification["status"])
        if config.OFFICIAL_SECTION_CATALOG_PATH:
            # Optional and fail-closed: without one exact official match, this
            # does nothing and the established OCR path is untouched.
            from app.official_sections import enrich_from_official_catalog, source_sha256
            official = enrich_from_official_catalog(
                roll_metadata, ocr_data.get("records", []),
                source_filename=document_location,
                catalog_path=config.OFFICIAL_SECTION_CATALOG_PATH,
                source_sha256=source_sha256(document_location))
            if official:
                say.info("Official section metadata verified",
                         section=official["section_number"], file=document_location.split("/")[-1])

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
            "extractor_version": "ocr-adapter-v1" if adapted is not None else "ocr-api-v1",
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
                    # Read from the OCR service when it reports one, NULL when
                    # it does not.
                    #
                    # This was the literal 2026. Nothing read a year off the PDF:
                    # the value was simply asserted, and then stamped onto every
                    # row of every roll the system will ever process, forever. A
                    # voter list dated 2019, 2027 or 2031 would be stored as 2026
                    # with no field anywhere recording that it was a guess.
                    #
                    # NULL is the honest value for "not read". It is visibly
                    # absent, so a row can never be mistaken for one whose year
                    # was actually recovered from the document. The column is
                    # already nullable, so nothing downstream has to change.
                    #
                    # The OCR service does not currently report a year --
                    # _extract_roll_header_metadata returns only state_code,
                    # ac_code, anubhag_code, anubhag_name and booth_code -- so
                    # today this is consistently None. That is correct, and it
                    # becomes correct-and-populated for free if the header ever
                    # gains a year, without touching this file again.
                    "roll_year": safe_int(roll_metadata.get("roll_year")),
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

    # ExtractionError must be caught before the generic handlers below. It is a
    # plain Exception subclass carrying a specific code and a retryable flag, so
    # the `except Exception` further down would catch it too and rewrite every
    # deliberate code as INTERNAL_ERROR with retryable=False.
    #
    # That is not cosmetic. `except (httpx.HTTPError, ValueError)` would also
    # swallow it -- EXTRACTION_SERVICE_UNAVAILABLE raises a ValueError-derived
    # payload through -- and a transient reader timeout would be reported as a
    # permanent internal error, so the unit is marked non-retryable and fails
    # permanently instead of being retried on the next dispatch. Re-raising the
    # specific errors first keeps the code and the retry decision intact.
    except ExtractionError:
        raise
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
