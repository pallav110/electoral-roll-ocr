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

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = timezone(timedelta(hours=5, minutes=30), name="IST")


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


# Columns a person reading a voter card does not ask for. They stay available
# on the export page, inside "More columns", and they start unticked.
_EXPORT_EXTRA = frozenset({
    "serial_number", "sno", "card_index",
    "relationship_type", "roll_format", "needs_review", "confidence",
    "state_code", "ac_code", "anubhag_status",
})


def export_is_extra(name: str) -> bool:
    """True for split name parts and the internal numbers on the export page."""
    return (
        name in _EXPORT_EXTRA
        or "_first_name_" in name
        or "_middle_name_" in name
        or "_sur_name_" in name
        or "_last_name_" in name
    )


def export_choices(columns, lang: str) -> list[dict]:
    """How each column is offered on the export page.

    The usual columns start ticked. Split name parts and the internal
    numbers start unticked, and a Hindi-only or English-only visit does
    not show the other language.
    """
    if lang not in {"both", "hi", "en"}:
        lang = "both"
    choices = []
    for column in columns:
        if column.name.endswith("_hi"):
            language = "hi"
        elif column.name.endswith("_en") or column.name == "relationship_type":
            language = "en"
        else:
            language = "common"
        visible = lang == "both" or language in {lang, "common"}
        extra = export_is_extra(column.name)
        choices.append({
            "name": column.name,
            "label": column.label,
            "language": language,
            "extra": extra,
            "checked": visible and not extra,
            "hidden": not visible,
        })
    return choices


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
            "Search the voters extracted from your uploaded electoral rolls."
        ),
        columns=[
            # First, so it is the frozen column and stays in view while the
            # rest of the row scrolls. A deleted card is a person who is no
            # longer a voter here; the whole row is tinted red in the UI.
            _c("is_deleted", "On the roll?",
               hint="\"Deleted\" means the roll has struck this card through. "
               "The person is no longer a voter here and is not counted."),
            _c("page_number", "Page", hint="Which page of the PDF", numeric=True),
            _c("source_row_number", "Row", hint="Position on the page", numeric=True),
            _c("voter_sr_no", "Roll number",
               hint="The number printed on the roll, kept as text so its "
               "formatting is preserved"),
            _c("serial_mark", "List mark",
               hint="The small box beside the serial on an addition-list "
               "card (परिवर्धन की सूची). A normal card does not print that "
               "box, so the cell says Not printed."),
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
            _c("ward_code", "Ward number",
               hint="The municipal ward number, printed on a Jaipur nigam "
               "roll. An assembly roll has no ward, so the cell says "
               "Not on this roll."),
            _c("roll_format", "Roll format"),
            _c("needs_review", "Check recommended",
               hint="Yes means one or more fields may need a human check"),
            _c("confidence", "Confidence", numeric=True),
            _c("state_code", "State code"),
            _c("ac_code", "Constituency code"),
            # Anubhag is the voter-roll term users see on the official PDF.
            # Do not show the legacy duplicate `section_*` columns above: old
            # imports did not always populate them and that made a filled
            # anubhag look falsely empty in Browse.
            #
            # The status sits with the pair it qualifies rather than up in the
            # roll-format block, because a blank anubhag number only reads as
            # "not applicable" when the reason is beside it.  On a municipal
            # ward roll there is no anubhag at all, and an unexplained blank
            # beside a filled name looks exactly like a failed extraction.
            _c("anubhag_status", "Anubhag status",
               hint="Not applicable for municipal ward rolls"),
            _c("anubhag_code", "Anubhag number", numeric=True,
               hint="अनुभाग संख्या — the polling section number printed on the roll"),
            _c("anubhag_name", "Anubhag name (Hindi)",
               hint="अनुभाग का नाम — the polling section/locality printed on the roll"),
            _c("booth_code", "Booth code"),
            # Record fingerprint, English source fingerprint, document,
            # session and work unit stay in the database for tracing. They
            # are not shown here: a person reading the roll cannot use them,
            # and an empty-looking cell is not the problem — the columns
            # themselves are. Source PDF and Saved at stay, because those
            # answer "which file" and "when".
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

# The data browser is a daily-user tool, not a database administration screen.
# Documents have their own purpose-built page; the remaining tables are
# pipeline diagnostics that would confuse a non-technical user and expose no
# action they can safely take. Keeping this allow-list separate preserves the
# internal registry for support/debugging code without making it a public UI.
PUBLIC_TABLE_ORDER: list[str] = [
    "electoral_records",
    "electoral_document_metadata",
]


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


# Booleans whose yes/no reads wrong to someone who does not know the column
# name. "is_deleted: yes" asks the reader to flip it in their head; "Deleted"
# does not. Shared by the browser cells and the spreadsheet export so the
# two say the same thing.
_BOOL_WORDS: dict[str, tuple[str, str]] = {
    "is_deleted": ("Deleted", "On the roll"),
    "needs_review": ("Check", "OK"),
    "is_valid": ("Passed", "Failed"),
}


def excel_cell(value, column: str | None = None):
    """A value openpyxl can store.

    Excel refuses a datetime that still carries a timezone. The browser shows
    the same instant in IST, so the workbook keeps that clock time with the
    timezone removed. A boolean with a known column is written as the same
    word the browser shows ("Deleted" / "On the roll"), not TRUE / FALSE.
    """
    if isinstance(value, bool) and column in _BOOL_WORDS:
        on, off = _BOOL_WORDS[column]
        return on if value else off
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_IST).replace(tzinfo=None)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    if value is None or isinstance(value, (str, int, float, bool, date)):
        return value
    return str(value)


def anubhag_display(row: dict) -> tuple[str, str]:
    """Return (anubhag number cell, anubhag name cell) for one row.

    A municipal ward roll has no assembly anubhag, so its anubhag number is
    an honest blank rather than a failed read.  Shown beside a filled name it
    is indistinguishable from an extraction failure, which is the confusion
    the pipeline goes out of its way to avoid.  Where the roll format says
    the anubhag does not apply, the ward is shown instead, and the name cell
    says why it is empty.
    """
    if (row.get("anubhag_status") or "") == "not_applicable":
        ward = cell_text(row.get("ward_code")).strip()
        name = cell_text(row.get("anubhag_name")).strip()
        number = f"Ward {ward}" if ward else "n/a (municipal ward roll)"
        if name:
            return number, f"{name} — no anubhag on a ward roll"
        return number, "n/a — municipal ward roll"
    if (row.get("anubhag_status") or "") == "header_needs_verification":
        code = cell_text(row.get("anubhag_code")).strip()
        # This is roll-level metadata, not a blank voter-card field.  Naming
        # that distinction prevents a single unreadable PDF header from being
        # mistaken for hundreds of corrupt voter records in Browse/export UI.
        return (
            code,
            "Header name needs verification from the source PDF",
        )
    return (
        cell_text(row.get("anubhag_code")),
        cell_text(row.get("anubhag_name")),
    )


def cell_text(value, column: str | None = None) -> str:
    """Render any column value as a short, safe string for the table.

    Long values (JSON, error text) are cut here so the browser never has to
    render a 40 KB cell; the full value stays in the row and the template
    reveals it on click. `column` lets a boolean be worded for its meaning
    rather than as yes/no.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        if column in _BOOL_WORDS:
            on, off = _BOOL_WORDS[column]
            return on if value else off
        return "yes" if value else "no"
    if isinstance(value, datetime):
        # Raw browser cells used to show server-local/UTC text; every
        # UI timestamp goes out in IST instead.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_IST).strftime("%d %b %Y, %H:%M:%S IST")
    if isinstance(value, (dict, list)):
        # Compact but still recognisable, e.g. {"age": "tesseract"}.
        text = ", ".join(f"{k}: {v}" for k, v in value.items()) \
            if isinstance(value, dict) else ", ".join(str(v) for v in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    # A dash (and a few OCR-equivalent glyphs) is how the electoral roll
    # explicitly says that no house number was supplied.  It is not a failed
    # extraction and must not be made to look like a blank or fabricated into
    # a number.  Keep the stored source value unchanged; this is presentation
    # only and is used by Browse and its JSON endpoint.
    if column in {"house_number_hi", "house_number_en"} and text in {"-", "—", "–", "«", ","}:
        return "Not provided in roll"
    if len(text) > 80:
        text = text[:80] + "…"
    return text


def house_number_display(value) -> str:
    """Return the user-facing house number without changing source data.

    ``None`` remains blank so a genuine OCR/data gap is still distinguishable
    from the roll's explicit '-' placeholder.
    """
    return cell_text(value, "house_number_hi") if value is not None else ""
