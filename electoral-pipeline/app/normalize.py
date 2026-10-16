"""Map the agreed electoral_v1 contract into queryable columns."""
import hashlib
import json
import logging
from datetime import date

from app.models import ElectoralDocumentMetadata, ElectoralRecord
from app.transliterate import gender_en, house_en as house_number_en, relation_en, source_hash, transliterate, translate_for_field


log = logging.getLogger(__name__)

# A visible marker makes an intentionally absent name component distinct from
# an accidental blank in Browse and spreadsheet exports.  It is applied only
# at persistence time, after OCR validation/transliteration, so it never
# becomes OCR input or a translated voter name.
MISSING_NAME_MARKER = "—"

# A normal voter card does not print the small box beside the serial. Storing
# this, rather than a blank, is what stops that cell looking like a missed read.
NO_LIST_MARK = "Not printed"

# An assembly roll has no municipal ward. A blank ward cell looks like a
# missed read; this says the roll does not print one. A Jaipur nigam
# (municipal ward) roll keeps the ward number it actually carries.
NO_WARD = "Not on this roll"

# The printed relation labels, keyed by the stable English key. Mirrors
# ``extractor._RELATION_KEY``; kept here so the English→Hindi direction does
# not have to import the extractor (which pulls in the OCR stack).
_RELATION_TYPE_HI = {
    "father": "पिता",
    "husband": "पति",
    "mother": "माता",
    "other": "अन्य",
}


def _int(value, field):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return int(value)


def _text(value):
    return None if value is None else str(value).strip() or None


def _name_cell(value):
    """Return a visible missing-value marker for stored name fields."""
    text = _text(value)
    return text if text is not None else MISSING_NAME_MARKER


def _confidence(value):
    """Coerce a reported confidence to a float in [0, 1], or None if unusable.

    Two things were wrong here, both found by running map_record over every
    input type rather than by reading it:

    1. `float(confidence)` raised on anything non-numeric. map_record is called
       in a list comprehension over a whole unit's records, so one record whose
       confidence OCRed as "" or "high" raised ValueError and aborted all 30 --
       the other 29 were fine and were thrown away with it. An unusable
       confidence is a problem with one field of one record; it must not
       destroy the unit.

    2. `bool` passed the range check, because `0 <= True <= 1` is True in
       Python. A record whose confidence was the JSON value `true` was stored
       with confidence True. _int() already refuses bool for the same reason;
       this does too.

    Out-of-range values are clamped rather than rejected. `2.5` is not evidence
    that the record is 250% certain -- it is evidence that whatever produced the
    number disagrees with the contract. Storing the clamped value keeps the
    record, and the caller still sees a validation error naming the problem, so
    the row is flagged for review rather than silently trusted.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN: the one value that is not equal to itself
        return None
    # Infinity is a valid float, so float() accepts it and a bare clamp would
    # turn it into 1.0 -- the *maximum* confidence, which is the most misleading
    # possible value for "the producer emitted infinity". NaN already became
    # None above; its infinities belong with it.
    if number in (float("inf"), float("-inf")):
        return None
    return number


def _in_unit_interval(number: float) -> bool:
    return 0.0 <= number <= 1.0


def _relation_type_en(value):
    """Relationship type → English.

    Accepts either the stable key the extractor already emits ("father") or a
    raw Hindi value ("पिता"), so a record is never stored with a Hindi
    relationship_type in an English-normalised column.
    """
    if not value:
        return None
    key = str(value).strip().lower()
    return relation_en(key) or key


def _relation_type_hi(value):
    """Relationship type → Hindi, the inverse of ``_relation_type_en``.

    ``relation_name`` and ``relationship_type`` are two columns holding one
    fact, but they are read from two different fields: the English side from
    ``common.relationship_type``, the Hindi from ``raw_record.relation_name``.
    A deployment that passed one and dropped the other left the English
    populated and the Hindi NULL on 13k rows, which reads on screen as a
    half-empty record rather than as missing data.

    This is the repair path for that: the four printed relation labels are a
    closed, 1:1 set (see ``extractor._RELATION_KEY``), so a known English key
    determines the Hindi word exactly. Anything unrecognised stays None
    rather than guessing -- an unmapped key means the vocabulary grew, and
    inventing a word would put a wrong relation on a real voter.
    """
    if not value:
        return None
    return _RELATION_TYPE_HI.get(str(value).strip().lower())


def map_metadata(data: dict, document_id, session_id):
    log.info(f"[MAP_METADATA] Starting metadata mapping for document_id={document_id}, session_id={session_id}")
    print(f"[MAP_METADATA] Starting metadata mapping for document_id={document_id}, session_id={session_id}")
    
    meta = data.get("document_metadata") or {}
    log.info(f"[MAP_METADATA] Document metadata keys: {list(meta.keys())}")
    print(f"[MAP_METADATA] Document metadata keys: {list(meta.keys())}")
    
    hi, en, common = (meta.get(key) or {} for key in ("hindi", "english", "common"))
    
    log.debug(f"[MAP_METADATA] Hindi keys: {list(hi.keys())}, English keys: {list(en.keys())}, Common keys: {list(common.keys())}")
    print(f"   Found document details in Hindi and English")
    
    # TODO(integration): Extend columns and aliases once your real metadata schema is final.
    publication_date = common.get("publication_date")
    
    log.info(f"[MAP_METADATA] Creating ElectoralDocumentMetadata object")
    print(f"   Preparing to save document information...")
    
    # English metadata is derived from the Hindi we store, for the same reason
    # record English is: one source of truth. An extractor that puts Devanagari
    # in en[] must not be able to decide what the English column says.
    def en_of(field: str):
        return transliterate(_text(hi.get(field))) or _text(en.get(field))

    metadata = ElectoralDocumentMetadata(
        document_id=document_id, session_id=session_id,
        state_hi=_text(hi.get("state")), district_hi=_text(hi.get("district")),
        assembly_constituency_hi=_text(hi.get("assembly_constituency")),
        polling_station_hi=_text(hi.get("polling_station")),
        polling_station_address_hi=_text(hi.get("polling_station_address")),
        state_en=en_of("state"), district_en=en_of("district"),
        assembly_constituency_en=en_of("assembly_constituency"),
        polling_station_en=en_of("polling_station"),
        polling_station_address_en=en_of("polling_station_address"),
        roll_year=common.get("roll_year"),
        publication_date=publication_date,
        assembly_constituency_number=common.get("assembly_constituency_number"),
        part_number=common.get("part_number"),
    )
    
    log.info(f"[MAP_METADATA] Metadata mapping complete")
    print(f"   Document information ready")
    return metadata


def map_record(item: dict, document_id, session_id, unit_id):
    log.debug(f"[MAP_RECORD] Mapping record for document_id={document_id}, session_id={session_id}, unit_id={unit_id}")
    print(f"[MAP_RECORD] Mapping record for document_id={document_id}, session_id={session_id}, unit_id={unit_id}")
    
    source, hi, en, common = (item.get(key) or {} for key in ("source", "hindi", "english", "common"))
    
    log.debug(f"[MAP_RECORD] Source keys: {list(source.keys())}, Hindi keys: {list(hi.keys())}, English keys: {list(en.keys())}, Common keys: {list(common.keys())}")
    print(f"   Processing voter record...")
    
    page = _int(source.get("page_number"), "page_number")
    row = _int(source.get("row_number"), "row_number")
    
    log.debug(f"[MAP_RECORD] Page={page}, Row={row}")
    print(f"   Voter on page {page}, row {row}")
    
    if page is None or row is None or row < 1:
        log.error(f"[MAP_RECORD] Invalid page or row: page={page}, row={row}")
        print(f"   Error: Invalid page or row number")
        raise ValueError("page_number and positive row_number are required")
    
    errors = []
    age = _int(common.get("age"), "age")
    if age is not None and not 0 <= age <= 120:
        errors.append("age outside 0–120")
        log.warning(f"[MAP_RECORD] Age validation failed: age={age}")
        print(f"[MAP_RECORD] Age validation failed: age={age}")
    
    # Read the raw value for the error message, but validate and store the
    # coerced one. The old code called float() unguarded and range-checked the
    # result, which raised on a non-numeric confidence and let bool through.
    raw_confidence = item.get("confidence")
    confidence = _confidence(raw_confidence)

    if confidence is None:
        if raw_confidence is not None:
            errors.append("confidence not a number")
            log.warning(f"[MAP_RECORD] Confidence unusable: confidence={raw_confidence!r}")
    elif not _in_unit_interval(confidence):
        # Out of range. Stored clamped rather than rejected: the record is still
        # worth keeping, and the note below is what puts it in front of a human.
        errors.append("confidence outside 0–1")
        clamped = max(0.0, min(1.0, confidence))
        confidence = clamped
        log.warning(f"[MAP_RECORD] Confidence out of range, clamped: confidence={raw_confidence!r} -> {confidence}")
    
    # Extract component name fields from OCR output (if available in raw_record)
    raw_record = item.get("raw_record", {})
    
    # OCR metadata fields
    sno = _int(raw_record.get("sno"), "sno")
    card_index = _int(raw_record.get("card_index"), "card_index")
    voter_sr_no = _text(raw_record.get("voter_sr_no"))
    serial_mark = _text(raw_record.get("serial_mark")) or NO_LIST_MARK
    is_deleted = bool(raw_record.get("is_deleted", False))
    state_code = _text(raw_record.get("state_code"))
    ac_code = _text(raw_record.get("ac_code"))
    anubhag_code = _int(raw_record.get("anubhag_code"), "anubhag_code")
    anubhag_name = _text(raw_record.get("anubhag_name"))
    ward_code = _text(raw_record.get("ward_code"))
    roll_format = _text(raw_record.get("roll_format")) or "assembly"
    if not ward_code and roll_format != "municipal_ward":
        ward_code = NO_WARD
    anubhag_status = _text(raw_record.get("anubhag_status"))
    booth_code = _text(raw_record.get("booth_code"))
    pdf_name = _text(raw_record.get("pdf_name"))
    needs_review = bool(raw_record.get("needs_review", False))
    review_reasons = raw_record.get("review_reasons") or []
    field_sources = raw_record.get("field_sources") or {}
    
    # Voter name components
    voter_first_name_hi = _text(raw_record.get("voter_first_name"))
    voter_middle_name_hi = _text(raw_record.get("voter_middle_name"))
    voter_sur_name_hi = _text(raw_record.get("voter_sur_name"))
    
    # If component fields not available in raw_record, fall back to joined name
    name_hi = _text(hi.get("name"))
    if not voter_first_name_hi and not voter_middle_name_hi and not voter_sur_name_hi and name_hi:
        # Try to split the joined name (simple space-based split)
        name_parts = name_hi.split()
        if len(name_parts) >= 1:
            voter_first_name_hi = name_parts[0]
        if len(name_parts) >= 2:
            voter_sur_name_hi = name_parts[-1]
        if len(name_parts) >= 3:
            voter_middle_name_hi = " ".join(name_parts[1:-1])
    
    # Reconstruct joined name from components for consistency
    name_hi = " ".join(filter(None, [voter_first_name_hi, voter_middle_name_hi, voter_sur_name_hi])) or name_hi
    
    # Relative name components
    # relation_name and relationship_type are one fact in two columns, read
    # from two different payload fields. Fall back to the English key so a
    # payload that carried only one of them still stores a whole record
    # rather than leaving the Hindi side silently NULL.
    relation_name = _text(raw_record.get("relation_name")) or _relation_type_hi(
        _text(common.get("relationship_type")))
    voter_father_first_name_hi = _text(raw_record.get("voter_father_first_name"))
    voter_father_middle_name_hi = _text(raw_record.get("voter_father_middle_name"))
    voter_father_last_name_hi = _text(raw_record.get("voter_father_last_name"))
    voter_husband_first_name_hi = _text(raw_record.get("voter_husband_first_name"))
    voter_husband_middle_name_hi = _text(raw_record.get("voter_husband_middle_name"))
    voter_husband_last_name_hi = _text(raw_record.get("voter_husband_last_name"))
    voter_mother_first_name_hi = _text(raw_record.get("voter_mother_first_name"))
    voter_mother_middle_name_hi = _text(raw_record.get("voter_mother_middle_name"))
    voter_mother_last_name_hi = _text(raw_record.get("voter_mother_last_name"))
    voter_other_first_name_hi = _text(raw_record.get("voter_other_first_name"))
    voter_other_middle_name_hi = _text(raw_record.get("voter_other_middle_name"))
    voter_other_last_name_hi = _text(raw_record.get("voter_other_last_name"))
    
    # If component fields not available, fall back to joined relative name
    relative_hi = _text(hi.get("relative_name"))
    
    gender_hi = _text(hi.get("gender"))
    # The section name and the anubhag name are the same header. Writing it
    # into only one of them is what left section_name blank on the addition
    # list after anubhag_name had already been filled.
    section_hi = _text(hi.get("section_name")) or anubhag_name
    house_hi = _text(hi.get("house_number"))

    if not name_hi and not en.get("name"):
        errors.append("name missing")
        log.warning(f"[MAP_RECORD] Name missing in both Hindi and English")
        print(f"   Warning: Voter name not found")

    identity = hashlib.sha256(f"{session_id}:{page}:{row}".encode()).hexdigest()
    # Hash of the Hindi this record's English was derived from. A later OCR
    # pass that corrects the Hindi changes this hash, which is how a stale
    # English value gets detected and regenerated rather than left to rot.
    translit_basis = source_hash(name_hi, relative_hi, gender_hi, section_hi, house_hi)

    log.debug(f"[MAP_RECORD] Record hash: {identity[:16]}..., errors: {errors}")
    print(f"   Record validated with {len(errors)} warnings")

    # English is derived from the Hindi we are about to store, never from
    # whatever the extractor happened to put in en[]. A missing or stale
    # English value must not be able to disagree with the Hindi source.
    # Gender and relationship are closed vocabularies; transliterating them
    # would store "purusa"/"pita" instead of the values a roll actually uses.
    # Name and relative_name use optional cloud translation with local caching
    # for common English spellings (e.g., "आकाश" → "Aakash"), falling back
    # to transliteration if translation is unavailable.
    
    # Translate component name fields
    voter_first_name_en = translate_for_field(voter_first_name_hi, 'name')
    voter_middle_name_en = translate_for_field(voter_middle_name_hi, 'name')
    voter_sur_name_en = translate_for_field(voter_sur_name_hi, 'name')
    
    # Translate relative name components
    voter_father_first_name_en = translate_for_field(voter_father_first_name_hi, 'relative_name')
    voter_father_middle_name_en = translate_for_field(voter_father_middle_name_hi, 'relative_name')
    voter_father_last_name_en = translate_for_field(voter_father_last_name_hi, 'relative_name')
    voter_husband_first_name_en = translate_for_field(voter_husband_first_name_hi, 'relative_name')
    voter_husband_middle_name_en = translate_for_field(voter_husband_middle_name_hi, 'relative_name')
    voter_husband_last_name_en = translate_for_field(voter_husband_last_name_hi, 'relative_name')
    voter_mother_first_name_en = translate_for_field(voter_mother_first_name_hi, 'relative_name')
    voter_mother_middle_name_en = translate_for_field(voter_mother_middle_name_hi, 'relative_name')
    voter_mother_last_name_en = translate_for_field(voter_mother_last_name_hi, 'relative_name')
    voter_other_first_name_en = translate_for_field(voter_other_first_name_hi, 'relative_name')
    voter_other_middle_name_en = translate_for_field(voter_other_middle_name_hi, 'relative_name')
    voter_other_last_name_en = translate_for_field(voter_other_last_name_hi, 'relative_name')
    
    # Joined name English (for backward compatibility)
    name_en = translate_for_field(name_hi, 'name')
    relative_en = translate_for_field(relative_hi, 'relative_name')
    
    section_en = transliterate(section_hi)
    # House numbers may be pure digits (pass through unchanged) or mixed
    # Devanagari/Latin like "7 बी" → "7 bi" / "8इ-526" → "8i-526".
    # Transliterate handles both: digits/punctuation stay, Devanagari converts.
    house_en = house_number_en(house_hi)
    gender_en_value = gender_en(gender_hi)
    relationship_type = _relation_type_en(_text(common.get("relationship_type")))

    # Keep semantic processing above this point on real values/None.  From
    # here on, all human-facing voter and relative name cells are explicit:
    # a single-name voter is ``Name — —`` in split columns, never an
    # ambiguous run of empty spreadsheet cells.
    name_hi, name_en = _name_cell(name_hi), _name_cell(name_en)
    voter_first_name_hi, voter_first_name_en = _name_cell(voter_first_name_hi), _name_cell(voter_first_name_en)
    voter_middle_name_hi, voter_middle_name_en = _name_cell(voter_middle_name_hi), _name_cell(voter_middle_name_en)
    voter_sur_name_hi, voter_sur_name_en = _name_cell(voter_sur_name_hi), _name_cell(voter_sur_name_en)
    relative_hi, relative_en = _name_cell(relative_hi), _name_cell(relative_en)
    voter_father_first_name_hi, voter_father_first_name_en = _name_cell(voter_father_first_name_hi), _name_cell(voter_father_first_name_en)
    voter_father_middle_name_hi, voter_father_middle_name_en = _name_cell(voter_father_middle_name_hi), _name_cell(voter_father_middle_name_en)
    voter_father_last_name_hi, voter_father_last_name_en = _name_cell(voter_father_last_name_hi), _name_cell(voter_father_last_name_en)
    voter_husband_first_name_hi, voter_husband_first_name_en = _name_cell(voter_husband_first_name_hi), _name_cell(voter_husband_first_name_en)
    voter_husband_middle_name_hi, voter_husband_middle_name_en = _name_cell(voter_husband_middle_name_hi), _name_cell(voter_husband_middle_name_en)
    voter_husband_last_name_hi, voter_husband_last_name_en = _name_cell(voter_husband_last_name_hi), _name_cell(voter_husband_last_name_en)
    voter_mother_first_name_hi, voter_mother_first_name_en = _name_cell(voter_mother_first_name_hi), _name_cell(voter_mother_first_name_en)
    voter_mother_middle_name_hi, voter_mother_middle_name_en = _name_cell(voter_mother_middle_name_hi), _name_cell(voter_mother_middle_name_en)
    voter_mother_last_name_hi, voter_mother_last_name_en = _name_cell(voter_mother_last_name_hi), _name_cell(voter_mother_last_name_en)
    voter_other_first_name_hi, voter_other_first_name_en = _name_cell(voter_other_first_name_hi), _name_cell(voter_other_first_name_en)
    voter_other_middle_name_hi, voter_other_middle_name_en = _name_cell(voter_other_middle_name_hi), _name_cell(voter_other_middle_name_en)
    voter_other_last_name_hi, voter_other_last_name_en = _name_cell(voter_other_last_name_hi), _name_cell(voter_other_last_name_en)

    # TODO(integration): Extend fields here alongside ElectoralRecord when your API adds fields.
    # OCR extracts anubhag_code/anubhag_name (from page 3 header), not section_number.
    # Use anubhag_code as section_number since they represent the same concept.
    section_number_val = _int(common.get("anubhag_code") or common.get("section_number"), "section_number")

    record = ElectoralRecord(
        document_id=document_id, session_id=session_id, extraction_unit_id=unit_id,
        page_number=page, source_row_number=row,
        serial_number=_int(common.get("serial_number"), "serial_number"), epic_number=_text(common.get("epic_number")),
        # OCR metadata fields
        sno=sno, card_index=card_index, voter_sr_no=voter_sr_no,
        serial_mark=serial_mark, is_deleted=is_deleted,
        state_code=state_code, ac_code=ac_code, anubhag_code=anubhag_code, anubhag_name=anubhag_name,
        ward_code=ward_code, roll_format=roll_format, anubhag_status=anubhag_status,
        booth_code=booth_code, pdf_name=pdf_name, needs_review=needs_review,
        review_reasons=review_reasons, field_sources=field_sources,
        # Voter name components
        name_hi=name_hi, name_en=name_en,
        voter_first_name_hi=voter_first_name_hi, voter_first_name_en=voter_first_name_en,
        voter_middle_name_hi=voter_middle_name_hi, voter_middle_name_en=voter_middle_name_en,
        voter_sur_name_hi=voter_sur_name_hi, voter_sur_name_en=voter_sur_name_en,
        # Relative name
        relative_name_hi=relative_hi, relative_name_en=relative_en,
        # The Hindi relation label (पिता, पति, माता, अन्य) as read by OCR.
        # This was computed at line 157 but never passed to the constructor,
        # so the column silently stayed NULL on every row while
        # relationship_type -- derived from the same value three files
        # earlier in extractor.py -- came through populated and hid it.
        relation_name=relation_name,
        # Father's name
        voter_father_first_name_hi=voter_father_first_name_hi, voter_father_first_name_en=voter_father_first_name_en,
        voter_father_middle_name_hi=voter_father_middle_name_hi, voter_father_middle_name_en=voter_father_middle_name_en,
        voter_father_last_name_hi=voter_father_last_name_hi, voter_father_last_name_en=voter_father_last_name_en,
        # Husband's name
        voter_husband_first_name_hi=voter_husband_first_name_hi, voter_husband_first_name_en=voter_husband_first_name_en,
        voter_husband_middle_name_hi=voter_husband_middle_name_hi, voter_husband_middle_name_en=voter_husband_middle_name_en,
        voter_husband_last_name_hi=voter_husband_last_name_hi, voter_husband_last_name_en=voter_husband_last_name_en,
        # Mother's name
        voter_mother_first_name_hi=voter_mother_first_name_hi, voter_mother_first_name_en=voter_mother_first_name_en,
        voter_mother_middle_name_hi=voter_mother_middle_name_hi, voter_mother_middle_name_en=voter_mother_middle_name_en,
        voter_mother_last_name_hi=voter_mother_last_name_hi, voter_mother_last_name_en=voter_mother_last_name_en,
        # Other relative's name
        voter_other_first_name_hi=voter_other_first_name_hi, voter_other_first_name_en=voter_other_first_name_en,
        voter_other_middle_name_hi=voter_other_middle_name_hi, voter_other_middle_name_en=voter_other_middle_name_en,
        voter_other_last_name_hi=voter_other_last_name_hi, voter_other_last_name_en=voter_other_last_name_en,
        # Other fields
        relationship_type=relationship_type, age=age,
        gender_hi=gender_hi, gender_en=gender_en_value,
        house_number_hi=house_hi, house_number_en=house_en,
        section_number=section_number_val,
        section_name_hi=section_hi, section_name_en=section_en,
        confidence=confidence, source_record_hash=identity, translit_source_hash=translit_basis,
        is_valid=not errors, validation_errors=errors,
    )
    
    log.debug(f"[MAP_RECORD] Record mapping complete, valid={not errors}")
    print(f"   Voter record ready to save")
    return record
