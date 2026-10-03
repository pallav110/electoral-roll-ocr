"""normalize_unit against a real database, not a mock.

The suite had no test that reached `normalize_unit`'s insert loop. That is how
`written += result.rowcount` shipped without `written = 0` above it: an
UnboundLocalError on the first record, failing every unit that reached
normalization. 308 tests passed with that bug present, because none of them
executed the line.

These tests run against the live Postgres in the compose stack, which is where
the `on_conflict_do_nothing` behaviour actually lives. They are skipped when the
database is unreachable, so `pytest` still works on a laptop with no stack -- but
in CI, where the database is a service container, they always run.

    docker compose exec -T db pg_isready          # confirm the stack is up
    python -m pytest tests/test_normalize_unit_db.py

What is being pinned:

- `records_extracted` is the number of rows that actually reached the table,
  not the number of records handed in. on_conflict_do_nothing discards a
  conflicting row silently, so a re-normalisation writes fewer rows than it
  intended and the old code reported the intent.
- A shortfall is logged and emitted as a NORMALIZATION_SHORTFALL event rather
  than passing unnoticed, because a truncated extraction is otherwise
  indistinguishable from a clean one.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _db_available() -> bool:
    try:
        from app.db import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="needs the compose Postgres; run the stack first"
)


@pytest.fixture
def scaffolding():
    """A document, a session and a unit, rolled back at the end.

    One transaction for the whole test: normalize_unit opens its own
    SessionLocal.begin() internally, so the scaffolding rows are committed
    first and then deleted explicitly. Nothing is left behind either way.
    """
    from app.db import SessionLocal

    from app.models import Document, ExtractionSession, ExtractionUnit
    from app.workflow import now

    doc_id = uuid.uuid4()
    session_id = uuid.uuid4()
    unit_id = uuid.uuid4()
    # uq_documents_hash is UNIQUE (source_type, file_hash), so a shared literal
    # hash collides across the three tests in this module. One per test.
    file_hash = uuid.uuid4().hex * 2

    with SessionLocal.begin() as db:
        db.add(
            Document(
                id=doc_id,
                source_type="upload",
                source_path=f"/tmp/test-roll-{doc_id}.pdf",
                file_name="test-roll.pdf",
                file_hash=file_hash,
                status="processing",
            )
        )
        db.add(
            ExtractionSession(
                id=session_id, document_id=doc_id, attempt_number=1, status="processing"
            )
        )
        db.add(
            ExtractionUnit(
                id=unit_id,
                session_id=session_id,
                unit_number=1,
                page_from=1,
                page_to=22,
                status="processing",
                worker_id="test-worker",
            )
        )

    yield doc_id, session_id, unit_id

    from sqlalchemy import delete, select

    from app.models import (
        ElectoralDocumentMetadata,
        ElectoralRecord,
        ProcessingEvent,
        RawResponse,
    )

    # Children first, in foreign-key order. normalize_unit writes an
    # ElectoralDocumentMetadata row for unit 1 (that is what the unit_number == 1
    # branch is for), and a RawResponse is written by the caller, so both have to
    # be cleared before the session can be -- otherwise teardown fails on the
    # constraint and leaks the whole fixture.
    with SessionLocal.begin() as db:
        db.execute(
            delete(ElectoralRecord).where(ElectoralRecord.session_id == session_id)
        )
        db.execute(
            delete(ElectoralDocumentMetadata).where(
                ElectoralDocumentMetadata.session_id == session_id
            )
        )
        db.execute(delete(RawResponse).where(RawResponse.session_id == session_id))
        db.execute(
            delete(ProcessingEvent).where(ProcessingEvent.session_id == session_id)
        )
        db.execute(delete(ExtractionUnit).where(ExtractionUnit.id == unit_id))
        db.execute(delete(ExtractionSession).where(ExtractionSession.id == session_id))
        db.execute(delete(Document).where(Document.id == doc_id))


def _payload(doc_id, session_id, unit_id, rows, version="ocr-api-v1"):
    """A minimal extraction payload shaped for normalize_unit."""
    return {
        "extractor_version": version,
        "page_from": 1,
        "page_to": 22,
        "document_metadata": {
            "hindi": {"state": "", "district": "", "assembly_constituency": ""},
            "english": {
                "state": "",
                "district": "",
                "assembly_constituency": "",
                "polling_station": "",
                "polling_station_address": "",
            },
            "common": {"roll_year": None},
        },
        "records": [
            {
                "source": {"page_number": page, "row_number": row},
                "hindi": {"name": f"परीक्षण{row}", "relative_name": "पिता", "house_number": "1"},
                "english": {},
                "common": {"epic_number": f"TEST{page:03d}{row:04d}", "age": 30},
                "raw_record": {"page_number": page, "row_number": row, "sno": row},
                "confidence": 0.9,
            }
            for page, row in rows
        ],
    }


def test_records_are_written_and_counted(scaffolding):
    """The happy path: written count equals intended count."""
    from sqlalchemy import func, select

    from app.models import ElectoralRecord, ExtractionUnit
    from app.workflow import normalize_unit

    doc_id, session_id, unit_id = scaffolding
    rows = [(3, 1), (3, 2), (3, 3)]
    normalize_unit(str(doc_id), str(session_id), str(unit_id), _payload(doc_id, session_id, unit_id, rows), "test-worker")

    from app.db import SessionLocal

    with SessionLocal() as db:
        actual = db.scalar(
            select(func.count())
            .select_from(ElectoralRecord)
            .where(ElectoralRecord.session_id == session_id)
        )
        unit = db.get(ExtractionUnit, unit_id)

    assert actual == 3, f"expected 3 rows in the table, found {actual}"
    assert unit.records_extracted == 3, (
        f"records_extracted reported {unit.records_extracted}, expected 3"
    )
    assert unit.status == "completed"


def _requeue(unit_id):
    """Put a unit back into the state normalize_unit accepts.

    The function opens with a re-entrancy guard -- it aborts unless the unit is
    still "processing" and still claimed by this worker -- so calling it twice in
    a row does nothing. That guard is correct and stays; a genuine retry resets
    the unit first, which is what this does.
    """
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with SessionLocal.begin() as db:
        unit = db.get(ExtractionUnit, unit_id, with_for_update=True)
        unit.status = "processing"
        unit.worker_id = "test-worker"
        unit.records_extracted = 0


def test_rerun_does_not_duplicate_and_reports_the_shortfall(scaffolding):
    """The regression this whole change is about.

    Normalising the same rows twice must leave 3 rows, not 6 --
    on_conflict_do_nothing is what prevents duplication. But the second run
    writes 0 new rows while handing in 3, and the old code stored
    records_extracted = len(records) = 3 on both runs, so the shortfall was
    invisible: the unit page would report three voters extracted from a pass
    that extracted none.
    """
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import ElectoralRecord, ExtractionUnit, ProcessingEvent
    from app.workflow import normalize_unit

    doc_id, session_id, unit_id = scaffolding
    rows = [(4, 1), (4, 2), (4, 3)]
    payload = _payload(doc_id, session_id, unit_id, rows)

    normalize_unit(str(doc_id), str(session_id), str(unit_id), payload, "test-worker")
    _requeue(unit_id)
    normalize_unit(str(doc_id), str(session_id), str(unit_id), payload, "test-worker")

    with SessionLocal() as db:
        actual = db.scalar(
            select(func.count())
            .select_from(ElectoralRecord)
            .where(ElectoralRecord.session_id == session_id)
        )
        unit = db.get(ExtractionUnit, unit_id)
        shortfalls = db.scalars(
            select(ProcessingEvent).where(
                ProcessingEvent.session_id == session_id,
                ProcessingEvent.event_type == "NORMALIZATION_SHORTFALL",
            )
        ).all()

    assert actual == 3, f"a re-run duplicated rows: {actual}, expected 3"
    assert unit.records_extracted == 0, (
        "the second run writes no new rows, so records_extracted must be 0 -- "
        f"got {unit.records_extracted}. Reporting len(records) here claims 3 "
        "voters were extracted when zero were."
    )
    assert len(shortfalls) == 1, (
        f"a silent shortfall must be recorded as an event; found {len(shortfalls)}"
    )
    assert "conflicted" in shortfalls[0].message


def test_records_extracted_never_exceeds_what_is_stored(scaffolding):
    """The invariant, stated directly: reported <= intended, always.

    This is the number the unit page and dashboard show a human, so it is worth
    pinning independently of any particular re-run shape.
    """
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import ElectoralRecord, ExtractionUnit
    from app.workflow import normalize_unit

    doc_id, session_id, unit_id = scaffolding
    rows = [(5, 1), (5, 2)]

    for _ in range(3):
        normalize_unit(
            str(doc_id),
            str(session_id),
            str(unit_id),
            _payload(doc_id, session_id, unit_id, rows),
            "test-worker",
        )
        _requeue(unit_id)

    with SessionLocal() as db:
        actual = db.scalar(
            select(func.count())
            .select_from(ElectoralRecord)
            .where(ElectoralRecord.session_id == session_id)
        )
        unit = db.get(ExtractionUnit, unit_id)

    assert actual == 2, f"repeated runs duplicated rows: {actual}, expected 2"
    assert unit.records_extracted <= len(rows), (
        f"records_extracted={unit.records_extracted} overstates the "
        f"{actual} rows actually stored"
    )