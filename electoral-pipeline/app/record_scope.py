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

from sqlalchemy import bindparam, case, func, select, text
from sqlalchemy.orm import Session as OrmSession

from app import config
from app.models import DocumentCover, ElectoralRecord, ExtractionSession

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


# Document statuses that mean extraction has been asked for and has not finished.
_ACTIVE_DOCUMENT_STATUSES = ("dispatched", "queued", "processing", "retry")
_OPEN_SESSION_STATUSES = ("created", "queued", "processing")

_MALE_HI = ("पुरुष", "पुरूष", "पु")
_FEMALE_HI = ("महिला", "स्त्री", "म")
_THIRD_HI = ("तृतीय लिंग", "तृतीय")


def _blank_pdf_stats(state: str) -> dict:
    return {
        "total": 0,
        "total_source": None,   # "cover" (printed on page 1) or "ocr" (session count)
        "extracted": 0,
        "male": 0,
        "female": 0,
        "third": 0,
        "deleted": 0,
        "deleted_male": 0,
        "deleted_female": 0,
        "deleted_third": 0,
        "review": 0,
        "reasons": [],
        "state": state,
        "cover": None,          # printed counts, once the cover has been read
        "cover_status": None,   # queued | processing | completed | failed | None
        "cover_retrying": False,  # failed, but under the attempt cap: the sweep will retry
        "cover_error": None,
        "mismatches": [],       # which of total/male/female/third disagree
    }


def _cover_view(cover) -> dict | None:
    if cover is None or cover.status != "completed":
        return None
    return {
        "total": cover.total,
        "male": cover.male,
        "female": cover.female,
        "third": cover.third_gender,
        "start_serial": cover.start_serial,
        "end_serial": cover.end_serial,
        "consistent": cover.counts_consistent,
    }


def _rollup_state(states: list[str]) -> str:
    if any(state == "mismatched" for state in states):
        return "mismatched"
    if states and all(state == "matched" for state in states):
        return "matched"
    if any(state in ("progress", "matched") for state in states):
        return "progress"
    return "pending"


def session_gender_counts(db: OrmSession, session_id) -> dict | None:
    """OCR counts for one attempt, keyed like the cover table.

    The cover's male / female / total leave struck-through cards out, so
    these counts do too. `deleted` is how many saved rows were struck
    through, and `rows` is everyone saved, deleted included.
    """
    if session_id is None:
        return None
    gender_en = func.lower(func.coalesce(ElectoralRecord.gender_en, ""))
    gender_hi = func.coalesce(ElectoralRecord.gender_hi, "")
    alive = ElectoralRecord.is_deleted.is_(False)
    deleted = ElectoralRecord.is_deleted.is_(True)
    is_male = (gender_en == "male") | gender_hi.in_(_MALE_HI)
    is_female = (gender_en == "female") | gender_hi.in_(_FEMALE_HI)
    is_third = gender_en.in_(("third gender", "third")) | gender_hi.in_(_THIRD_HI)
    row = db.execute(
        select(
            func.count().label("rows"),
            func.count().filter(deleted).label("deleted"),
            func.count().filter(deleted, is_male).label("deleted_male"),
            func.count().filter(deleted, is_female).label("deleted_female"),
            func.count().filter(deleted, is_third).label("deleted_third"),
            func.count().filter(alive).label("total"),
            func.count().filter(alive, is_male).label("male"),
            func.count().filter(alive, is_female).label("female"),
            func.count().filter(alive, is_third).label("third_gender"),
        ).where(ElectoralRecord.session_id == session_id)
    ).one()
    return {
        "rows": int(row.rows),
        "deleted": int(row.deleted),
        "deleted_male": int(row.deleted_male),
        "deleted_female": int(row.deleted_female),
        "deleted_third": int(row.deleted_third),
        "total": int(row.total),
        "male": int(row.male),
        "female": int(row.female),
        "third_gender": int(row.third_gender),
    }


def batch_extraction_stats(db: OrmSession, documents) -> dict:
    """Per-PDF counts for one batch page, from the attempt a read would show.

    Total records is what the cover page prints in its section-4 table once
    that page has been read; until then it is the count the pipeline recorded
    for the attempt. Extracted records is the number of voter rows actually
    stored, and deleted is how many of those are struck through. The cover
    total leaves deletions out, so a PDF is "matched" when the rows that are
    not deleted agree with the printed total and with the printed male /
    female / third-gender counts. Gender and review counts come from those
    same rows, so a retry cannot double them.
    """
    by_document: dict[str, dict] = {}
    for document in documents:
        doc_status = str(getattr(document, "status", "") or "")
        state = "progress" if doc_status in _ACTIVE_DOCUMENT_STATUSES else "pending"
        by_document[str(document.id)] = _blank_pdf_stats(state)
    if not documents:
        return {"by_document": {}, "total": 0, "extracted": 0, "state": "pending", "covers_read": 0}

    document_ids = [document.id for document in documents]
    covers = {
        str(cover.document_id): cover
        for cover in db.scalars(
            select(DocumentCover).where(DocumentCover.document_id.in_(document_ids))
        ).all()
    }
    for doc_key, row in by_document.items():
        cover = covers.get(doc_key)
        row["cover_status"] = cover.status if cover is not None else None
        if cover is not None and cover.status == "failed":
            row["cover_retrying"] = (cover.attempt_count or 0) < config.COVER_MAX_ATTEMPTS
            row["cover_error"] = cover.error
        row["cover"] = _cover_view(cover)
        if row["cover"] and row["cover"]["total"] is not None:
            row["total"] = row["cover"]["total"]
            row["total_source"] = "cover"
    completed = ExtractionSession.status.in_(COMPLETED_SESSION_STATUSES)
    has_rows = (
        select(ElectoralRecord.id)
        .where(ElectoralRecord.session_id == ExtractionSession.id)
        .exists()
    )
    ranked = (
        select(
            ExtractionSession.document_id.label("document_id"),
            ExtractionSession.id.label("session_id"),
            ExtractionSession.status.label("status"),
            ExtractionSession.records_extracted.label("records_extracted"),
            func.row_number().over(
                partition_by=ExtractionSession.document_id,
                order_by=(
                    case((completed, 1), else_=0).desc(),
                    ExtractionSession.attempt_number.desc(),
                ),
            ).label("rn"),
        )
        .where(ExtractionSession.document_id.in_(document_ids), has_rows)
        .subquery()
    )
    chosen = db.execute(
        select(
            ranked.c.document_id,
            ranked.c.session_id,
            ranked.c.status,
            ranked.c.records_extracted,
        ).where(ranked.c.rn == 1)
    ).all()
    if not chosen:
        states = [row["state"] for row in by_document.values()]
        return {
            "by_document": by_document,
            "total": sum(row["total"] for row in by_document.values()),
            "extracted": 0,
            "state": _rollup_state(states),
            "covers_read": sum(1 for row in by_document.values() if row["cover"]),
        }

    session_of = {row.session_id: row for row in chosen}
    gender_en = func.lower(func.coalesce(ElectoralRecord.gender_en, ""))
    gender_hi = func.coalesce(ElectoralRecord.gender_hi, "")
    alive = ElectoralRecord.is_deleted.is_(False)
    deleted = ElectoralRecord.is_deleted.is_(True)
    is_male = (gender_en == "male") | gender_hi.in_(_MALE_HI)
    is_female = (gender_en == "female") | gender_hi.in_(_FEMALE_HI)
    is_third = gender_en.in_(("third gender", "third")) | gender_hi.in_(_THIRD_HI)
    counts = db.execute(
        select(
            ElectoralRecord.session_id,
            func.count().label("extracted"),
            func.count().filter(deleted).label("deleted"),
            func.count().filter(deleted, is_male).label("deleted_male"),
            func.count().filter(deleted, is_female).label("deleted_female"),
            func.count().filter(deleted, is_third).label("deleted_third"),
            func.count().filter(ElectoralRecord.needs_review.is_(True)).label("review"),
            func.count().filter(alive, is_male).label("male"),
            func.count().filter(alive, is_female).label("female"),
            func.count().filter(alive, is_third).label("third"),
        )
        .where(ElectoralRecord.session_id.in_(list(session_of)))
        .group_by(ElectoralRecord.session_id)
    ).all()
    counted = {row.session_id: row for row in counts}

    reasons_by_session: dict = {session_id: [] for session_id in session_of}
    reason_rows = db.execute(
        text(
            """
            SELECT session_id, reason, count(*) AS n
            FROM electoral_records
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(review_reasons) = 'array' THEN review_reasons
                    ELSE '[]'::jsonb
                END
            ) AS reason
            WHERE session_id IN :session_ids
              AND needs_review
            GROUP BY session_id, reason
            ORDER BY n DESC, reason
            """
        ).bindparams(bindparam("session_ids", expanding=True)),
        {"session_ids": list(session_of)},
    ).all()
    for row in reason_rows:
        reasons_by_session.setdefault(row.session_id, []).append((row.reason, int(row.n)))

    for chosen_row in chosen:
        row = by_document[str(chosen_row.document_id)]
        counted_row = counted.get(chosen_row.session_id)
        extracted = int(counted_row.extracted) if counted_row is not None else 0
        reported = int(chosen_row.records_extracted or 0)
        row["extracted"] = extracted
        row["deleted"] = int(counted_row.deleted) if counted_row is not None else 0
        row["deleted_male"] = int(counted_row.deleted_male) if counted_row is not None else 0
        row["deleted_female"] = int(counted_row.deleted_female) if counted_row is not None else 0
        row["deleted_third"] = int(counted_row.deleted_third) if counted_row is not None else 0
        row["male"] = int(counted_row.male) if counted_row is not None else 0
        row["female"] = int(counted_row.female) if counted_row is not None else 0
        row["third"] = int(counted_row.third) if counted_row is not None else 0
        row["review"] = int(counted_row.review) if counted_row is not None else 0
        row["reasons"] = reasons_by_session.get(chosen_row.session_id, [])
        if row["total_source"] != "cover":
            row["total"] = reported or extracted
            row["total_source"] = "ocr"

        if chosen_row.status in _OPEN_SESSION_STATUSES:
            row["state"] = "progress"
            continue
        cover = row["cover"]
        if cover and cover["total"] is not None:
            # The printed table counts electors still on the roll. Struck-
            # through cards are saved, and shown in their own column, but
            # they are not part of this comparison.
            living = extracted - row["deleted"]
            mismatches = []
            if living != cover["total"]:
                mismatches.append("total")
            for key in ("male", "female", "third"):
                if cover.get(key) is not None and row[key] != cover[key]:
                    mismatches.append(key)
            row["mismatches"] = mismatches
            row["state"] = "matched" if not mismatches else "mismatched"
        else:
            row["state"] = "matched" if row["total"] == extracted else "mismatched"
            if row["state"] == "mismatched":
                row["mismatches"] = ["total"]

    states = [row["state"] for row in by_document.values()]
    return {
        "by_document": by_document,
        "total": sum(row["total"] for row in by_document.values()),
        "extracted": sum(row["extracted"] for row in by_document.values()),
        "state": _rollup_state(states),
        "covers_read": sum(1 for row in by_document.values() if row["cover"]),
    }