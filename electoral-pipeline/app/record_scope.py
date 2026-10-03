"""Which extraction attempt a read should see.

`electoral_records` rows are keyed by (session_id, page_number,
source_row_number), not by voter. Every retry of a document opens a *new*
session and writes a *new* set of rows, so a document that has been retried
once holds two complete copies of its roll -- same page, same card, same
serial number, different `session_id`.

    SELECT ... WHERE document_id = X          ->  1062 rows for 531 voters
    SELECT ... WHERE session_id = <newest>    ->   531 rows

Nothing in the read or export paths filtered on the session, so the document
page listed every voter twice and `/documents/{id}/export` wrote 1062 CSV
lines for a 531-voter roll. An operator reconciling a roll against the paper
list has to know which duplicate is the real one, and there is no way to tell
from the CSV.

Which attempt wins
------------------
The newest attempt that actually produced rows, preferring one that ran to
completion. Reasoning backwards from what the columns can tell us:

- A **completed** session is authoritative even when a later attempt exists.
  Retries happen *because* something went wrong; a later "failed" or
  "partial" attempt is strictly worse information than the completed one
  before it, and a CSV full of half-extracted rows is worse than the good
  copy already in the table.
- Otherwise the **newest attempt with any rows at all** wins, so a retry that
  is mid-flight and has normalised two units so far is better than showing
  the previous attempt's rows as though nothing had been retried.
- A document with no rows anywhere returns nothing. It never falls back to an
  older attempt, because an attempt that wrote nothing wrote nothing.

This is deliberately not "newest session wins". That rule is simpler and it
is wrong: it discards a completed extraction in favour of a retry that
crashed.

An attempt that is genuinely superseded is not lost, only hidden: the unit
and session pages still address any session by id, and the export carries the
attempt so a reader can tell which one they are looking at.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.models import ElectoralRecord, ExtractionSession

# Statuses that mean "this attempt produced trustworthy rows".
# "completed" for the whole session; "partial" is a session whose units were
# individually retried to exhaustion, so its rows are the best that attempt
# has. Neither is "failed" (nothing usable) nor "processing"/"created"
# (incomplete and still moving).
COMPLETED_SESSION_STATUSES = ("completed", "partial")


def attempt_with_records(db: OrmSession, document_id) -> ExtractionSession | None:
    """The session whose records a read for `document_id` should return.

    Returns None when the document has produced no rows in any attempt, which
    callers must treat as "no records" rather than "fall back".
    """
    has_rows = (
        select(ExtractionSession.id)
        .join(ElectoralRecord, ElectoralRecord.session_id == ExtractionSession.id)
        .where(ExtractionSession.document_id == document_id)
        .exists()
    )

    # Fast path: a completed attempt. Several can exist in principle (an
    # operator can retry a completed document via retry_document), so
    # attempt_number breaks the tie toward the later one.
    row = db.execute(
        select(ExtractionSession)
        .where(
            ExtractionSession.document_id == document_id,
            ExtractionSession.status.in_(COMPLETED_SESSION_STATUSES),
            has_rows,
        )
        .order_by(ExtractionSession.attempt_number.desc())
        .limit(1)
    ).scalars().first()
    if row is not None:
        return row

    # Fallback: the newest attempt that wrote anything at all.
    return db.execute(
        select(ExtractionSession)
        .where(ExtractionSession.document_id == document_id, has_rows)
        .order_by(ExtractionSession.attempt_number.desc())
        .limit(1)
    ).scalars().first()


def records_for_document(
    db: OrmSession, document_id, *, limit: int | None = None, offset: int = 0
) -> tuple[list[ElectoralRecord], ExtractionSession | None]:
    """Records for `document_id` from one attempt, plus which attempt it was.

    Ordering is (page_number, source_row_number) so the result reads in the
    order the cards are printed on the page -- the same order
    `_voter_card_rects()` yields them in the OCR service.

    A session of None means the document has no records in any attempt. The
    caller gets an empty list, never another attempt's rows.
    """
    session = attempt_with_records(db, document_id)
    if session is None:
        return [], None

    query = (
        select(ElectoralRecord)
        .where(ElectoralRecord.session_id == session.id)
        .order_by(ElectoralRecord.page_number, ElectoralRecord.source_row_number)
    )
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return list(db.scalars(query).all()), session


def count_records_for_document(db: OrmSession, document_id) -> int:
    """How many records the scoped attempt holds.

    Counts the same set `records_for_document` returns. A separate COUNT
    scoped to the same session_id, because a COUNT filtered on document_id
    would silently report the duplicate-inflated number again.
    """
    session = attempt_with_records(db, document_id)
    if session is None:
        return 0
    return db.scalar(
        select(func.count())
        .select_from(ElectoralRecord)
        .where(ElectoralRecord.session_id == session.id)
    ) or 0