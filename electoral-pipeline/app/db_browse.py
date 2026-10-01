"""Registry of database tables and columns for the read-only browser.

Two rules make this safe to expose to a non-technical user:

1. The browser never builds SQL from a name the user typed. Every table and
   column a visitor can reach is listed here, and a request for anything not
   in this file is refused. That makes SQL injection structurally impossible
   rather than filtered-for.
2. Nothing here writes. The browser is a viewer; corrections go through the
   pipeline, so a stray click can never alter the roll.

The friendly names are display only. They never reach the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    """One database column and how to present it."""

    name: str
    label: str
    hint: str = ""
    # Long values (JSON blobs, free text) are shown shortened until clicked.
    expandable: bool = False
    # Right-align numbers so digits line up for scanning.
    numeric: bool = False


@dataclass(frozen=True)
class Table:
    """One database table, its purpose, and its columns in display order."""

    name: str
    label: str
    description: str
    columns: list[Column] = field(default_factory=list)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


def _c(name: str, label: str, *, hint: str = "", expandable: bool = False,
       numeric: bool = False) -> Column:
    """Build a column. hint is keyword-only so a hint string can never be
    passed positionally and collide with the expandable flag."""
    return Column(name=name, label=label, hint=hint,
                  expandable=expandable, numeric=numeric)


# Shared column groups. The voter table repeats a first/middle/last pattern for
# the voter and for four kinds of relative, so those are generated rather than
# written out 24 times.
#
# The two families do NOT use the same suffix. The voter's own name ends in
# sur_name (voter_sur_name_hi), while the four relatives end in last_name
# (voter_father_last_name_hi). Generating one pattern for both produced eight
# columns that do not exist, which took /browse down with a 500.
def _name_parts(prefix: str, owner: str, final: str = "sur") -> list[Column]:
    """first/middle/last name columns in both languages, for one person.

    `final` is the surname column's middle word: 'sur' for the voter,
    'last' for a relative.
    """
    out = []
    for part in ("first", "middle", final):
        label = "surname" if part == final else f"{part} name"
        out.append(_c(f"{prefix}_{part}_name_hi", f"{owner} {label} (Hindi)"))
        out.append(_c(f"{prefix}_{part}_name_en", f"{owner} {label} (English)"))
    return out


_TABLES: list[Table] = [
    Table(
        name="electoral_records",
        label="Voter records",
        description=(
            "The voter list itself -- one row per card on a page. This is the "
            "table people usually want."
        ),
        columns=[
            _c("page_number", "Page", hint="Which page of the PDF", numeric=True),
            _c("source_row_number", "Row", hint="Position on the page", numeric=True),
            _c("voter_sr_no", "Roll number",
               hint="The number printed on the roll, kept as text so its "
               "formatting is preserved"),
            _c("epic_number", "Voter ID",
               hint="The 10-character voter identity number"),
            _c("serial_number", "Serial", numeric=True),
            _c("sno", "S.No", numeric=True),
            _c("card_index", "Card", hint="Card position, 0-29", numeric=True),
            _c("name_hi", "Voter name (Hindi)"),
            _c("name_en", "Voter name (English)"),
            *_name_parts("voter", "Voter"),
            _c("relation_name", "Relative type",
               hint="पिता / पति / माता / अन्य -- father, husband, mother, other"),
            _c("relative_name_hi", "Relative name (Hindi)"),
            _c("relative_name_en", "Relative name (English)"),
            _c("relationship_type", "Relationship (English)"),
            *_name_parts("voter_father", "Father", final="last"),
            *_name_parts("voter_husband", "Husband", final="last"),
            *_name_parts("voter_mother", "Mother", final="last"),
            *_name_parts("voter_other", "Other relative", final="last"),
            _c("age", "Age", numeric=True),
            _c("gender_hi", "Gender (Hindi)"),
            _c("gender_en", "Gender (English)"),
            _c("house_number_hi", "House number (Hindi)"),
            _c("house_number_en", "House number (English)"),
            _c("section_number", "Section", numeric=True),
            _c("section_name_hi", "Section name (Hindi)"),
            _c("section_name_en", "Section name (English)"),
            _c("is_deleted", "Marked deleted"),
            _c("is_valid", "Passed checks"),
            _c("needs_review", "Needs a human check",
               hint="Set when OCR was unsure about some field"),
            _c("review_reasons", "Why it needs review",
               hint="Click to expand", expandable=True),
            _c("validation_errors", "Validation problems",
               hint="Click to expand", expandable=True),
            _c("field_sources", "Where each value came from",
               hint="Click to expand", expandable=True),
            _c("confidence", "Confidence", numeric=True),
            _c("state_code", "State code"),
            _c("ac_code", "Constituency code"),
            _c("anubhag_code", "Anuvargh code", numeric=True),
            _c("anubhag_name", "Anuvargh name"),
            _c("booth_code", "Booth code"),
            _c("source_record_hash", "Record fingerprint"),
            _c("translit_source_hash", "English source fingerprint",
               hint="Changes when the Hindi is corrected, so a stale English "
               "value can be spotted"),
            _c("document_id", "Document"),
            _c("session_id", "Session"),
            _c("extraction_unit_id", "Work unit"),
            _c("pdf_name", "Source PDF"),
            _c("created_at", "Saved at"),
        ],
    ),
    Table(
        name="documents",
        label="Documents",
        description="One row per PDF or spreadsheet the pipeline picked up.",
        columns=[
            _c("file_name", "File name"),
            _c("source_type", "Source type", hint="pdf or xlsx"),
            _c("source_path", "File path"),
            _c("status", "Status", hint="Waiting / running / finished / failed"),
            _c("priority", "Priority", hint="Lower runs first", numeric=True),
            _c("attempt_count", "Attempts so far", numeric=True),
            _c("max_attempts", "Attempts allowed", numeric=True),
            _c("file_size", "File size (bytes)", numeric=True),
            _c("file_hash", "File fingerprint"),
            _c("last_error", "Last error"),
            _c("processing_started_at", "Started at"),
            _c("processing_completed_at", "Finished at"),
            _c("discovered_at", "Found at"),
            _c("created_at", "Added at"),
            _c("updated_at", "Last change"),
            _c("id", "Document ID"),
        ],
    ),
    Table(
        name="extraction_sessions",
        label="Sessions",
        description=(
            "One attempt at reading a document. A document gets a new session "
            "each time it is retried."
        ),
        columns=[
            _c("attempt_number", "Attempt", numeric=True),
            _c("status", "Status"),
            _c("pages_total", "Pages in document", numeric=True),
            _c("pages_processed", "Pages read", numeric=True),
            _c("records_extracted", "Voters found", numeric=True),
            _c("records_failed", "Voters failed", numeric=True),
            _c("processing_time_ms", "Time taken (ms)", numeric=True),
            _c("worker_id", "Which worker"),
            _c("error_code", "Error code"),
            _c("error_message", "Error message"),
            _c("started_at", "Started at"),
            _c("completed_at", "Finished at"),
            _c("document_id", "Document"),
            _c("id", "Session ID"),
        ],
    ),
    Table(
        name="extraction_units",
        label="Work units",
        description=(
            "A document is split into units of a few pages each, and each unit "
            "is read separately so a failure only costs those pages."
        ),
        columns=[
            _c("unit_number", "Unit", numeric=True),
            _c("page_from", "First page", numeric=True),
            _c("page_to", "Last page", numeric=True),
            _c("status", "Status"),
            _c("attempt_count", "Attempts", numeric=True),
            _c("records_extracted", "Voters found", numeric=True),
            _c("worker_id", "Which worker"),
            _c("error_code", "Error code"),
            _c("error_message", "Error message"),
            _c("queued_at", "Queued at"),
            _c("started_at", "Started at"),
            _c("completed_at", "Finished at"),
            _c("session_id", "Session"),
            _c("id", "Unit ID"),
        ],
    ),
    Table(
        name="electoral_document_metadata",
        label="Roll details",
        description=(
            "The state, constituency and polling station printed on the roll. "
            "Read from the first pages and copied onto every record."
        ),
        columns=[
            _c("state_hi", "State (Hindi)"),
            _c("state_en", "State (English)"),
            _c("district_hi", "District (Hindi)"),
            _c("district_en", "District (English)"),
            _c("assembly_constituency_number", "Constituency number", numeric=True),
            _c("assembly_constituency_hi", "Constituency (Hindi)"),
            _c("assembly_constituency_en", "Constituency (English)"),
            _c("part_number", "Part number", numeric=True),
            _c("polling_station_hi", "Polling station (Hindi)"),
            _c("polling_station_en", "Polling station (English)"),
            _c("polling_station_address_hi", "Station address (Hindi)"),
            _c("polling_station_address_en", "Station address (English)"),
            _c("roll_year", "Roll year", numeric=True),
            _c("publication_date", "Published on"),
            _c("document_id", "Document"),
            _c("session_id", "Session"),
        ],
    ),
    Table(
        name="processing_events",
        label="Activity log",
        description="Everything the pipeline did, in order, for each document.",
        columns=[
            _c("created_at", "When"),
            _c("service", "Which part"),
            _c("event_type", "What happened"),
            _c("message", "Message"),
            _c("details", "Extra detail", hint="Click to expand",
               expandable=True),
            _c("document_id", "Document"),
            _c("session_id", "Session"),
            _c("extraction_unit_id", "Work unit"),
        ],
    ),
    Table(
        name="extraction_raw_responses",
        label="Raw OCR replies",
        description=(
            "The unedited reply from the OCR service, kept so any saved record "
            "can be traced back to what was actually read."
        ),
        columns=[
            _c("received_at", "Received at"),
            _c("extractor_version", "OCR version"),
            _c("session_id", "Session"),
            _c("extraction_unit_id", "Work unit"),
            _c("raw_response", "Full reply",
               hint="The OCR service's answer, exactly as received "
                    "(click to expand)", expandable=True),
            _c("id", "Reply ID"),
        ],
    ),
]

TABLES: dict[str, Table] = {t.name: t for t in _TABLES}
TABLE_ORDER: list[str] = [t.name for t in _TABLES]


def get_table(name: str) -> Table | None:
    """Look up a table by name, or None if it is not exposed."""
    return TABLES.get(name)


def columns_present_in(table: Table, actual: set[str]) -> list[str]:
    """Registry columns that the live table does not have.

    Used to drop stale entries at request time rather than letting the query
    fail with a 500: a rename in a migration should cost one column, not the
    whole browser.
    """
    return [c.name for c in table.columns if c.name not in actual]


def default_table() -> Table:
    """The table to open on: the voter records."""
    return TABLES["electoral_records"]


def cell_text(value) -> str:
    """Render any column value as a short, safe string for the table.

    Long values (JSON, error text) are cut here so the browser never has to
    render a 40 KB cell; the full value stays in the row and the template
    reveals it on click.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        # Compact but still recognisable, e.g. {"age": "tesseract"}.
        text = ", ".join(f"{k}: {v}" for k, v in value.items()) \
            if isinstance(value, dict) else ", ".join(str(v) for v in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > 80:
        text = text[:80] + "…"
    return text
