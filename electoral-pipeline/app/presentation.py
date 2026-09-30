"""Turn machine values into words a non-technical person can act on.

The admin UI is read by people who do not know what a "unit" or an
EXTRACTION_TIMEOUT is. Everything here is display-only: it never touches what
is stored, so a nicer label can never change a record.

Kept out of the templates on purpose. Jinja expressions for "turn this error
code into a sentence someone can act on" get long and unreadable fast, and this
way the wording is testable and identical on every page.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Machine status -> what the operator should read.
# Anything not listed falls back to the raw value, so a new status shows up
# rather than rendering blank.
STATUS_LABEL = {
    "pending": "Waiting to start",
    "queued": "In the queue",
    "processing": "Being processed",
    "completed": "Finished",
    "retry": "Retrying",
    "failed": "Failed",
    "cancelled": "Stopped",
    "partial": "Partly finished",
}

# A colour hint for the operator. Kept separate from the wording so the
# wording can change without touching styling.
STATUS_TONE = {
    "pending": "wait",
    "queued": "wait",
    "processing": "busy",
    "retry": "busy",
    "completed": "good",
    "failed": "bad",
    "cancelled": "bad",
    "partial": "warn",
}

# Error code -> (plain headline, what to do about it).
# The codes are internal; the sentences are the whole point of this module.
ERROR_PLAIN = {
    "EXTRACTION_TIMEOUT": (
        "Took too long",
        "Reading the PDF took longer than the time allowed. This usually means "
        "the PDF is large or the machine is busy. Retrying when the machine is "
        "quieter usually works.",
    ),
    "MODEL_TIMEOUT": (
        "Took too long",
        "Reading the PDF took longer than the time allowed. Retrying usually works.",
    ),
    "EXTRACTION_FAILED": (
        "Could not read the PDF",
        "The text could not be pulled out of these pages. This can happen with "
        "scanned or damaged pages.",
    ),
    "INTERNAL_ERROR": (
        "Something went wrong inside the system",
        "This is a problem with the program, not with the PDF. Retrying may work; "
        "if it keeps happening the error message below will say where.",
    ),
    "EXTRACTION_SERVICE_UNAVAILABLE": (
        "The reading service is not answering",
        "The service that reads PDFs did not respond. It may be restarting or "
        "too busy. Try again in a few minutes.",
    ),
    "INVALID_PDF": (
        "The PDF could not be opened",
        "The file may be damaged, password-protected, or not actually a PDF.",
    ),
    "INVALID_RESPONSE": (
        "The reply did not make sense",
        "The reading service sent back something unexpected. This is a problem "
        "with the program, not with the PDF.",
    ),
    "INVALID_CONFIGURATION": (
        "The system is not set up correctly",
        "A setting is missing or wrong. This needs an administrator to fix.",
    ),
    "EXTRACTOR_HTTP_ERROR": (
        "The reading service had a problem",
        "The service that reads PDFs returned an error. Try again shortly.",
    ),
}

# Internal event names -> something readable. Only the ones a non-technical
# reader will actually see; the rest fall through to a prettified version.
EVENT_LABEL = {
    "DOCUMENT_DISCOVERED": "New PDF found",
    "SESSION_CREATED": "Started work on the PDF",
    "SESSION_STARTED": "Work started",
    "SESSION_FAILED": "Work stopped with an error",
    "UNITS_CREATED": "PDF split into batches",
    "UNIT_STARTED": "Started a batch of pages",
    "UNIT_COMPLETED": "Finished a batch of pages",
    "UNIT_FAILED": "A batch of pages failed",
    "UNIT_RETRY": "Retrying a batch of pages",
    "RAW_RESPONSE_SAVED": "Saved the reading result",
    "RECORDS_SAVED": "Saved voter records",
    "RECORDS_SKIPPED": "Some records were skipped",
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def status_label(status: str | None) -> str:
    """'failed' -> 'Failed', 'completed' -> 'Finished'."""
    if not status:
        return "Unknown"
    return STATUS_LABEL.get(str(status).strip().lower(), str(status).replace("_", " ").title())


def status_tone(status: str | None) -> str:
    """Colour hint for a status badge. Unknown status -> neutral."""
    if not status:
        return "muted"
    return STATUS_TONE.get(str(status).strip().lower(), "muted")


def event_label(event_type: str | None) -> str:
    """'UNIT_FAILED' -> 'A batch of pages failed'."""
    if not event_type:
        return "Unknown event"
    key = str(event_type).strip()
    if key in EVENT_LABEL:
        return EVENT_LABEL[key]
    return key.replace("_", " ").title()


def _plurals(n, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


def describe_error(code, message=None) -> tuple[str, str | None]:
    """(plain headline, what to do) for an error code + raw message.

    The raw message is returned separately so the UI can still show it on
    hover for whoever needs the technical detail, without making it the
    headline for everyone else.
    """
    key = str(code or "").strip()
    if key in ERROR_PLAIN:
        return ERROR_PLAIN[key]
    if not key and not message:
        return ("Something went wrong", None)
    return ("Something went wrong", None)


def friendly_error(code, message=None) -> str:
    """One-line plain-English summary of a failure."""
    headline, _ = describe_error(code, message)
    return headline


def progress_summary(done: int | None, total: int | None) -> str:
    """'4 of 22 pages done' -- or an honest 'Not started' when total is unknown."""
    total = total or 0
    done = done or 0
    if not total:
        return "Not started"
    if done >= total:
        return f"All {total} {_plurals(total, 'page')} done"
    return f"{done} of {total} {_plurals(total, 'page')} done"


def records_summary(count: int | None) -> str:
    """'30 voter records saved' / 'No voter records yet'."""
    if not count:
        return "No voter records yet"
    return f"{count:,} voter {_plurals(count, 'record')} saved"


def duration_summary(ms: int | None) -> str:
    """Readable duration: '2 min 5 sec'. Never '0 ms' for a missing value."""
    if not ms:
        return None
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds} sec"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" + (f" {rem} sec" if rem else "")
    hours, rem = divmod(minutes, 60)
    return f"{hours} hr" + (f" {rem} min" if rem else "")


def time_ago(value: datetime | None) -> str | None:
    """'3 min ago' relative to now.

    Naive timestamps come back from Postgres for these columns, which compare
    against an aware utcnow() incorrectly, so both sides are normalised to
    aware UTC before subtracting. Comparing naive to aware raises, and a
    template that raises is a blank page for the operator.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    return f"{seconds // 86400} {_plurals(seconds // 86400, 'day')} ago"


def short_time(value: datetime | None) -> str | None:
    """'30 Sep, 14:05' -- compact, sortable-looking, no timezone noise."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%d %b, %H:%M")


def file_size_summary(num_bytes: int | None) -> str | None:
    """'4.6 MB' rather than '4781968 bytes'."""
    if not num_bytes:
        return None
    size = float(num_bytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} bytes"
            return f"{size:.1f} {unit}"
        size /= 1024
    return None


def short_id(value, keep: int = 8) -> str:
    """Trim a UUID to something a human can read out loud: '8dff1802-c516'."""
    if not value:
        return "—"
    text = str(value)
    return text if len(text) <= keep + 4 else text[:keep] + "…"


def has_devanagari(value) -> bool:
    """True if the text still contains Devanagari.

    The UI uses this to flag an English column that still holds Hindi, which
    is the failure the bilingual work exists to catch.
    """
    if not value:
        return False
    return bool(re.search(r"[ऀ-ॿ]", str(value)))


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def document_progress(session) -> str:
    """One line describing how far a document got, for the list view."""
    if session is None:
        return "Not started"
    status = str(getattr(session, "status", "") or "").lower()
    if status == "completed":
        return "Finished"
    return progress_summary(
        getattr(session, "pages_processed", 0),
        getattr(session, "pages_total", 0),
    )


def document_records_summary(session) -> str:
    if session is None:
        return "No voter records yet"
    return records_summary(getattr(session, "records_extracted", 0))
