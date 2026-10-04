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


def event_detail(details=None, message: str | None = None) -> str | None:
    """The Details cell for an event row, as one readable phrase.

    The three history tables disagreed about which column held the detail:
    dashboard.html rendered `details`, unit.html and document.html rendered
    `message`. That made the same events look informative on one page and
    blank on another. Both are read here so every page agrees.

    `message` is NULL for 8 of the 10 event types in a live roll -- the
    payloads live in `details` -- which is why two of the three tables were
    almost entirely dashes. Details first because it is the structured
    record; message second because it is the free-text one, and only when
    details has nothing to say.
    """
    parts: list[str] = []

    if isinstance(details, dict):
        for key, value in details.items():
            # A nested object is not worth a cell; the readable part is the
            # top-level counts (records, records_expected).
            if isinstance(value, (dict, list)):
                continue
            label = str(key).replace("_", " ")
            parts.append(f"{label}: {value}")
    elif details is not None and str(details).strip() not in {"", "None", "null"}:
        # `not in (None, "", {})` looks like it drops empty values but does
        # not: 0 is falsy and fails the membership test, so it rendered "0".
        # Comparing the stringified form drops blanks without swallowing a
        # legitimate zero.
        parts.append(str(details))

    text = (message or "").strip()
    if text and text not in {"None", "null"}:
        parts.append(text)

    return " · ".join(parts) if parts else None


def duration_between(started: datetime | None, completed: datetime | None) -> str | None:
    """Readable elapsed time for a job, computed from the timestamps it has.

    `duration_summary(ms)` takes a stored millisecond count, and only
    ExtractionSession has that column. ExtractionUnit has no such column --
    models.py declares processing_time_ms on the *session*, and the unit class
    begins below it -- so every template that asked a unit for
    `processing_time_ms` got Jinja's Undefined, which is falsy, and the
    `or '--'` fallback rendered a dash for all 11 units. The timings were
    never missing: started_at and completed_at are both populated.

    Returns None when the job has not finished, so a running unit reads
    "still going" rather than a misleading "0 sec". Sub-second work rounds
    up to "under a second" instead of "0 sec", because 0 sec reads as a
    failure rather than as speed.
    """
    if not started or not completed:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    ms = int((completed - started).total_seconds() * 1000)
    if ms < 0:
        # Clock skew between the two writes, or a host whose clock moved.
        # A negative duration is meaningless; reporting nothing beats
        # reporting "-3 sec".
        return None
    if ms < 1000:
        return "under a second"
    return duration_summary(ms)


def _aware(value: datetime | str | None) -> datetime | None:
    """A timestamp as an aware UTC datetime, whatever shape it arrived in.

    Postgres hands these columns back naive on some paths and the JSON status
    endpoint sends strings, so both have to be accepted. Mixing a naive and an
    aware datetime raises TypeError, and a helper that raises inside a Jinja
    render is a blank page for the operator.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# A unit in one of these states has stopped doing work, so its pages are
# settled. `retry` is deliberately absent: a retrying unit will run again.
_SETTLED = {"completed"}


def live_progress(units, pages_total=None, at: datetime | None = None) -> dict:
    """How far a session has actually got, from per-unit state, right now.

    Why this exists rather than reading session.pages_processed: both
    `pages_processed` and `records_extracted` on ExtractionSession are
    written in exactly one place -- finalize(), which runs at the very end.
    A page bound to those columns shows 0 for the whole run and then jumps
    to the total, which is the same complaint as showing 90 records after
    three pages, only slower.

    So the numbers here are built from the unit rows instead, and they come
    in two clearly separated flavours:

      *_settled   -- really happened. Counted, committed, past tense.
      *_estimate  -- how far the running batch has got, worked out from the
                     rate the finished batches actually achieved.

    The distinction is the whole point. workflow.py already refuses to
    present a derived number as a stored one, and a counter that ticks up
    every five seconds while nothing has been written must not sit under a
    label saying "saved". So the estimate drives the headline; the settled
    count rides underneath it, and is what the word "saved" is attached to.

    The rate is measured, not assumed: seconds-per-page and records-per-page
    both come from batches that have actually finished in this same session.
    Before the first batch finishes there is no rate, so there is no
    estimate either -- reporting a real number there would mean inventing
    one, and `can_estimate` says so explicitly rather than showing 0.
    """
    now = _aware(at) if at else datetime.now(timezone.utc)
    units = list(units or [])

    total_pages = pages_total or sum(
        (u.page_to - u.page_from + 1) for u in units if u.page_from and u.page_to
    ) or None

    settled = [u for u in units if str(getattr(u, "status", "")) in _SETTLED]
    running = [u for u in units if str(getattr(u, "status", "")) == "processing"]

    pages_settled = sum(u.page_to - u.page_from + 1 for u in settled)
    records_settled = sum(int(getattr(u, "records_extracted", 0) or 0) for u in settled)

    # Rate comes only from batches that actually finished. A batch that
    # failed says nothing about how fast a good one runs.
    #
    # Both rates are weighted totals over a denominator, never a sum of
    # per-batch ratios. Summing ratios double-counts: two finished batches of
    # 90 records over 10 pages each give 9 + 9 = 18 records per page, which
    # would inflate every projection from the second batch onwards.
    seconds_per_page = None
    records_per_page = None
    timed_pages = timed_seconds = 0
    record_pages = record_total = 0
    for u in settled:
        pages = (u.page_to - u.page_from + 1) if (u.page_from and u.page_to) else 0
        if pages <= 0:
            continue
        records_in_unit = int(getattr(u, "records_extracted", 0) or 0)
        if records_in_unit:
            record_pages += pages
            record_total += records_in_unit
        start, end = _aware(getattr(u, "started_at", None)), _aware(getattr(u, "completed_at", None))
        if start and end:
            elapsed = (end - start).total_seconds()
            if elapsed >= 0:
                timed_pages += pages
                timed_seconds += elapsed
    if timed_pages and timed_seconds > 0:
        seconds_per_page = timed_seconds / timed_pages
    if record_pages:
        records_per_page = record_total / record_pages

    running_unit = running[0] if running else None
    running_pages = 0
    elapsed_seconds = None
    fraction = None

    if running_unit is not None:
        running_pages = running_unit.page_to - running_unit.page_from + 1
        start = _aware(getattr(running_unit, "started_at", None))
        if start:
            elapsed_seconds = max(0.0, (now - start).total_seconds())

    if seconds_per_page and running_pages and elapsed_seconds is not None:
        expected = seconds_per_page * running_pages
        if expected > 0:
            fraction = min(0.95, elapsed_seconds / expected)
            # 0.95 rather than 1.0: a batch that is 100% of the way through
            # its expected time is not finished, it is due. Letting the bar
            # reach full before the row actually flips would overstate the
            # work done, which is the one thing this function must not do.

    pages_estimate = pages_settled + (fraction * running_pages if fraction else 0)
    records_estimate = None
    if records_per_page and fraction:
        records_estimate = int(records_settled + fraction * running_pages * records_per_page)

    eta_seconds = None
    if seconds_per_page and total_pages:
        remaining = max(0, total_pages - pages_estimate)
        eta_seconds = int(remaining * seconds_per_page)

    done = bool(units) and not running and len(settled) == len(units)
    if done:
        state = "done"
    elif running:
        state = "working"
    elif settled:
        state = "between"
    else:
        state = "waiting"

    return {
        "state": state,
        "pages_total": total_pages,
        "pages_settled": pages_settled,
        "pages_estimate": round(pages_estimate, 1),
        "records_settled": records_settled,
        "records_estimate": records_estimate,
        "can_estimate": bool(fraction),
        "running_unit_number": getattr(running_unit, "unit_number", None) if running_unit else None,
        "running_pages": running_pages,
        "elapsed_seconds": int(elapsed_seconds) if elapsed_seconds is not None else None,
        "seconds_per_page": round(seconds_per_page, 2) if seconds_per_page else None,
        "records_per_page": round(records_per_page, 2) if records_per_page else None,
        "eta_seconds": eta_seconds,
    }


def fuse_progress(progress: dict | None, scan: dict | None,
                  page_from: int | None = None,
                  page_to: int | None = None) -> dict:
    """Merge the OCR card counter into the database's batch numbers.

    Two services, two clocks, and no way for either to see the other's state.
    Postgres knows batches; the OCR process knows cards. The database numbers
    are the truth about what has been *saved*; the OCR counter is the only
    thing that moves between two database writes, because it is written per
    card and the database only hears about a unit when all ten pages of it are
    done.

    So the OCR counter is used for motion and the database for accuracy, and
    they are only allowed to be combined when the two are provably about the
    same work. A session page open for a session whose units are all still
    queued would otherwise display another session's climbing card count --
    a wrong number shown confidently, which is what workflow.py:840-844
    refuses to do anywhere else in this codebase.

    The binding is checked, not assumed:

      1. the OCR run must be live (not `done`), and must have actually started;
      2. `active_page` must fall inside this running unit's page range.

    With the worker at --concurrency=1 there is exactly one unit in flight in
    the whole system, so a page inside that range is this session's work and
    nothing else. The staleness guard below closes the remaining gap: a
    finished counter left over from an earlier request still names its last
    page, so it still passes the range test after the unit ends.
    """

    base = dict(progress or {})
    if not base:
        base = {
            "state": "waiting", "pages_total": None, "pages_settled": 0,
            "pages_estimate": 0, "records_settled": 0, "records_estimate": None,
            "can_estimate": False, "running_unit_number": None,
            "running_pages": 0, "elapsed_seconds": None,
            "seconds_per_page": None, "records_per_page": None,
            "eta_seconds": None,
        }

    scan = scan or {}
    # Read the OCR snapshot defensively. Anything unexpected in the payload
    # degrades to "no card motion" rather than propagating into a render.
    cards_done = _int_or_none(scan.get("cards_done")) or 0
    cards_records = _int_or_none(scan.get("cards_records")) or 0
    cards_total = _int_or_none(scan.get("cards_total")) or 0
    active_page = scan.get("active_page")
    idle_s = _int_or_none(scan.get("idle_s"))
    if idle_s is not None and float(idle_s) != idle_s:
        idle_s = None
    ocr_done = bool(scan.get("done"))

    # `ocr_done` alone is not enough to reject a snapshot. Between two units
    # `_OCR_LOCK` releases and the next request resets the counter: at that
    # moment `done` is False again while `active_page` still names the
    # *previous* unit's last page, which is inside that previous unit's range.
    # And a counter never exercised at all reads done=False, idle_s=0.0,
    # cards_done=0. Requiring both `not done` and a fresh timestamp rejects
    # all three: a leftover counter, a between-units gap, and a cold one.
    fresh = idle_s is not None and float(idle_s) <= 30
    ocr_live = (not ocr_done) and fresh

    # The page test. With the worker at --concurrency=1 there is exactly one
    # unit in flight in the whole system, so a page inside this running unit's
    # range is this session's work and nothing else.
    in_range = (
        page_from is not None and page_to is not None
        and isinstance(active_page, int)
        and page_from <= active_page <= page_to
    )

    base["cards_live"] = bool(ocr_live and in_range)
    base["cards_done"] = cards_done if base["cards_live"] else 0
    base["cards_records"] = cards_records if base["cards_live"] else 0
    base["cards_total"] = cards_total if base["cards_live"] else 0
    base["active_page"] = active_page if base["cards_live"] else None

    # A card count is worth showing only once there is a denominator. Until
    # the first page of a unit has finished, `cards_total` is 0 because
    # `_progress_page_done` is what adds to it -- dividing by it would be a
    # division by zero presented as a progress bar.
    base["cards_known_total"] = base["cards_total"] if base["cards_total"] > 0 else None

    # The headline number. Cards read is the honest live figure; it counts work
    # that genuinely happened, unlike the rate-projected `records_estimate`.
    # They are not the same unit: a blank lattice slot is a card read that
    # produces no record, so cards_done >= cards_records always, and on a tail
    # page the two diverge sharply (page 22 yields 30 cards and 0 records).
    base["records_headline"] = (
        base["cards_records"] if base["cards_live"]
        else base.get("records_estimate") if base.get("can_estimate")
        else base.get("records_settled") or 0
    )
    base["headline_is_estimate"] = bool(
        not base["cards_live"] and base.get("can_estimate")
    )
    return base


def eta_summary(seconds: int | None) -> str | None:
    """'about 4 min left' / 'less than a minute left' / None when unknown.

    Deliberately hedged with "about". The number is a projection from the
    rate so far, and the first batch of a session is not representative of
    the rest -- a roll that opens with a slow page reads fast afterwards.
    """
    if seconds is None or seconds < 0:
        return None
    if seconds < 60:
        return "less than a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min left"
    hours, rem = divmod(minutes, 60)
    return f"about {hours} hr {rem} min left" if rem else f"about {hours} hr left"


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
