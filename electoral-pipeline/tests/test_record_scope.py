"""A retried document must not list every voter once per attempt.

`electoral_records` has no voter key. Its uniqueness constraint is
(session_id, page_number, source_row_number), so each extraction attempt
writes its own complete set of cards. A document retried once therefore
holds two full copies of its roll -- identical page numbers, card indices
and serial numbers, differing only in `session_id`.

Neither the document page nor the CSV export filtered on the session:

    records = db.scalars(
        select(ElectoralRecord)
        .where(ElectoralRecord.document_id == doc.id)      # every attempt
        ...
    )

so both returned each voter twice with nothing to distinguish the copies. On
a 531-voter roll the export wrote 1062 lines.

These tests run against the live Postgres in the compose stack and skip when
it is unreachable.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="needs the compose Postgres; run the stack first"
)


def _make_document(file_hash):
    from app.db import SessionLocal

    from app.models import Document

    doc_id = uuid.uuid4()
    with SessionLocal.begin() as db:
        db.add(
            Document(
                id=doc_id,
                source_type="upload",
                source_path=f"/tmp/test-scope-{doc_id}.pdf",
                file_name="test-scope.pdf",
                file_hash=file_hash,
                status="completed",
            )
        )
    return doc_id


def _add_attempt(document_id, attempt_number, status, rows):
    """One extraction attempt: a session plus `rows` (page, card) cards.

    The serial number is derived from page/card, not from the attempt, so the
    two attempts describe genuinely identical voters -- which is what makes
    the duplicate-inflation the test is about visible.
    """
    from app.db import SessionLocal

    from app.models import ElectoralRecord, ExtractionSession

    session_id = uuid.uuid4()
    with SessionLocal.begin() as db:
        db.add(
            ExtractionSession(
                id=session_id,
                document_id=document_id,
                attempt_number=attempt_number,
                status=status,
            )
        )
        # Flush the session on its own. The records below reference it by id,
        # and SQLAlchemy sorts INSERTs by ORM relationship -- there is none
        # between these two (a bare UUID column, not a mapped association), so
        # without an intervening flush it emits the records first and the
        # foreign key rejects them.
        db.flush()

    with SessionLocal.begin() as db:
        for page, card in rows:
            db.add(
                ElectoralRecord(
                    document_id=document_id,
                    session_id=session_id,
                    page_number=page,
                    source_row_number=card,
                    voter_sr_no=f"{page:03d}{card:04d}",
                    epic_number=f"EPIC{page:03d}{card:04d}",
                    name_hi=f"नाम{card}",
                )
            )
    return session_id


@pytest.fixture
def document():
    """A document whose roll was extracted twice, then retried a third time."""
    from app.db import SessionLocal

    from app.models import ElectoralDocumentMetadata, ElectoralRecord, ExtractionSession, ProcessingEvent

    # uq_documents_hash is UNIQUE (source_type, file_hash): one hash per test.
    file_hash = uuid.uuid4().hex * 2
    doc_id = _make_document(file_hash)

    rows = [(3, 1), (3, 2), (3, 3)]
    first = _add_attempt(doc_id, 1, "completed", rows)
    second = _add_attempt(doc_id, 2, "failed", rows)
    third = _add_attempt(doc_id, 3, "created", rows[:1])

    yield doc_id, first, second, third

    session_ids = [first, second, third]
    with SessionLocal.begin() as db:
        db.execute(
            delete(ElectoralRecord).where(ElectoralRecord.session_id.in_(session_ids))
        )
        db.execute(
            delete(ElectoralDocumentMetadata).where(
                ElectoralDocumentMetadata.session_id.in_(session_ids)
            )
        )
        db.execute(
            delete(ProcessingEvent).where(ProcessingEvent.session_id.in_(session_ids))
        )
        db.execute(
            delete(ExtractionSession).where(ExtractionSession.id.in_(session_ids))
        )
        from app.models import Document

        db.execute(delete(Document).where(Document.id == doc_id))


def test_only_one_attempt_is_returned(document):
    """The regression: three attempts, so three copies of every voter."""
    from app.db import SessionLocal

    from app.record_scope import records_for_document

    doc_id, first, second, third = document

    with SessionLocal() as db:
        records, session = records_for_document(db, doc_id)

    assert len(records) == 3, (
        f"returned {len(records)} records for a 3-voter roll. Every attempt "
        "wrote its own copy, so an unscoped read returns one voter per attempt."
    )
    assert session.id == first, (
        "attempt 1 completed; attempts 2 and 3 are failed and in-flight. The "
        f"completed one is authoritative, but got {session.id}."
    )


def test_a_completed_attempt_beats_a_later_failed_one(document):
    """Why this is not simply "newest session wins".

    A retry exists because something went wrong. Preferring the newest
    attempt would discard a good extraction in favour of one that crashed,
    and show an operator an empty or half-full roll.
    """
    from app.db import SessionLocal

    from app.models import Document
    from app.record_scope import attempt_with_records

    doc_id, first, _, _ = document

    with SessionLocal() as db:
        chosen = attempt_with_records(db, doc_id)
        # Also prove the document itself is otherwise untouched.
        assert db.get(Document, doc_id).status == "completed"

    assert chosen.attempt_number == 1


def test_count_matches_the_returned_rows(document):
    """A count scoped to document_id would report the inflated number.

    The document page shows "N of M". If M counted all attempts while N
    listed one, the page would claim records exist that it is not showing.
    """
    from app.db import SessionLocal

    from app.record_scope import count_records_for_document, records_for_document

    doc_id, _, _, _ = document

    with SessionLocal() as db:
        records, _ = records_for_document(db, doc_id)
        counted = count_records_for_document(db, doc_id)

    assert counted == len(records) == 3


def test_newest_attempt_wins_when_none_completed(document):
    """With no completed attempt, the newest one that wrote rows wins.

    A retry that is mid-flight and has normalised one unit is still better
    evidence than the previous attempt's rows presented as current.
    """
    from app.db import SessionLocal

    from app.models import ExtractionSession

    from app.record_scope import attempt_with_records

    doc_id, first, _, third = document

    with SessionLocal.begin() as db:
        # Demote attempt 1 so the "completed" fast path cannot match.
        db.execute(
            ExtractionSession.__table__.update()
            .where(ExtractionSession.id == first)
            .values(status="failed")
        )

    with SessionLocal() as db:
        chosen = attempt_with_records(db, doc_id)

    assert chosen.id == third, (
        "with no completed attempt the newest one carrying rows should win, "
        f"got attempt {chosen.attempt_number}"
    )


def test_document_with_no_records_returns_nothing_not_another_attempt():
    """No rows anywhere means no rows -- never a silent fallback."""
    from app.db import SessionLocal

    from app.models import Document

    from app.record_scope import count_records_for_document, records_for_document

    doc_id = _make_document(uuid.uuid4().hex * 2)

    try:
        with SessionLocal() as db:
            records, session = records_for_document(db, doc_id)
            counted = count_records_for_document(db, doc_id)

        assert records == []
        assert session is None
        assert counted == 0
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(Document).where(Document.id == doc_id))


def test_attempt_written_nothing_is_not_chosen():
    """An attempt that produced no rows cannot be the answer.

    A session row exists from the moment of dispatch, well before any OCR
    runs. Choosing it would blank the document page for the duration of
    every extraction.
    """
    from app.db import SessionLocal

    from app.record_scope import attempt_with_records

    file_hash = uuid.uuid4().hex * 2
    doc_id = _make_document(file_hash)
    empty = _add_attempt(doc_id, 1, "completed", [])
    populated = _add_attempt(doc_id, 2, "completed", [(1, 1), (1, 2)])

    try:
        with SessionLocal() as db:
            chosen = attempt_with_records(db, doc_id)

        assert chosen.id == populated, (
            "a completed attempt with zero rows was chosen over one that has "
            "records; dispatch creates the session before any OCR runs"
        )
    finally:
        from app.models import ElectoralRecord, ExtractionSession

        with SessionLocal.begin() as db:
            db.execute(
                delete(ElectoralRecord).where(ElectoralRecord.session_id.in_([empty, populated]))
            )
            db.execute(delete(ExtractionSession).where(ExtractionSession.id.in_([empty, populated])))
            from app.models import Document

            db.execute(delete(Document).where(Document.id == doc_id))


def test_pagination_offset_is_honoured(document):
    """The document page passes limit=100; offset must compose with it."""
    from app.db import SessionLocal

    from app.record_scope import records_for_document

    doc_id, _, _, _ = document

    with SessionLocal() as db:
        first_page, _ = records_for_document(db, doc_id, limit=2)
        second_page, _ = records_for_document(db, doc_id, limit=2, offset=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert first_page[0].id != second_page[0].id