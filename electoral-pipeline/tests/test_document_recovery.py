"""A document must not be stranded because the broker dropped its message.

`recover()` had a branch meant to republish a "queued" document whose session
had done no work -- the case where the message was accepted by the broker and
then lost, so no worker will ever pick it up. It read:

    if (doc.status == "queued" and doc.locked_at and doc.locked_at < queued_cutoff
            and session and session.status == "created"):

and **nothing in the codebase ever wrote `locked_at`**. The column was declared
in models.py since the table was created and was always NULL, so the third
clause was never true and the branch never ran. Worse, the branch also wrote
`doc.locked_at = now()` -- it satisfied its own precondition -- so the author
expected the column to be filled elsewhere.

The consequence: a document whose publish was lost sat at "queued" for good.
`dispatch_documents()` selects only "pending" and "retry", so no future dispatch
would touch it either. Nothing would ever time it out, retry it, or tell anyone.

The unit-level equivalent has always worked, because `dispatch_units()` does
write `queued_at`. This test pins the document-level fix so the two paths stay
symmetrical.

These tests run against the live Postgres in the compose stack and skip when it
is unreachable, so `pytest` still works on a laptop with no stack.
"""
from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

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


@pytest.fixture
def stranded_document(monkeypatch):
    """A document published to a broker that never delivered the message.

    Built by calling `dispatch_documents()`, not by hand-writing rows. That is
    deliberate: the bug was that dispatch never stamped `locked_at`, so a
    hand-built fixture that sets it itself passes with the bug still present --
    it would be testing its own setup. Going through the real dispatch means
    this fixture *cannot* have a publish timestamp unless the production code
    wrote one.

    The broker swallows every message, which is the whole failure being modelled.
    """
    from app.db import SessionLocal

    from app import tasks, workflow
    from app.models import Document

    class _Swallowed:
        def __init__(self):
            self.calls: list[tuple] = []

        def delay(self, *args):
            self.calls.append(args)
            return None

    broker = _Swallowed()
    monkeypatch.setattr(tasks, "process_document", broker)
    monkeypatch.setattr(tasks, "process_unit", broker)

    doc_id = uuid.uuid4()
    # uq_documents_hash is UNIQUE (source_type, file_hash), so a shared literal
    # hash would collide across tests in this module. One per test.
    file_hash = uuid.uuid4().hex * 2

    with SessionLocal.begin() as db:
        db.add(
            Document(
                id=doc_id,
                source_type="upload",
                source_path=f"/tmp/test-stranded-{doc_id}.pdf",
                file_name="test-stranded.pdf",
                file_hash=file_hash,
                status="pending",
            )
        )

    dispatched = workflow.dispatch_documents()
    assert dispatched >= 1, "dispatch_documents() took nothing"

    with SessionLocal() as db:
        document = db.get(Document, doc_id)
        session_id = document.current_session_id
        assert document.status == "queued", (
            f"after dispatch the document is {document.status!r}, expected 'queued'"
        )
    assert session_id is not None, "dispatch left no current session"
    assert any(len(call) == 2 and call[0] == str(doc_id) for call in broker.calls), (
        f"dispatch never published this document; saw {broker.calls}"
    )

    yield doc_id, session_id

    from sqlalchemy import delete

    from app.models import ExtractionSession as Session
    from app.models import ExtractionUnit, ProcessingEvent

    with SessionLocal.begin() as db:
        db.execute(
            delete(ProcessingEvent).where(ProcessingEvent.session_id == session_id)
        )
        db.execute(delete(ExtractionUnit).where(ExtractionUnit.session_id == session_id))
        db.execute(delete(Session).where(Session.id == session_id))
        db.execute(delete(Document).where(Document.id == doc_id))


def _age_the_publish(db, doc_id, seconds):
    """Push locked_at into the past, as if the publish had happened long ago.

    Refuses to work when locked_at is NULL. A version of this helper that simply
    assigned a timestamp would paper over the exact bug under test: dispatch
    never stamping locked_at is *why* the column was always NULL, so a test that
    writes the value itself proves nothing about whether production writes it.
    That mistake was made here once already -- all five tests passed with the
    publish-time stamp removed from dispatch_documents().

    To age a publish, production must first have recorded one.
    """
    from app.models import Document
    from app.workflow import now

    doc = db.get(Document, doc_id, with_for_update=True)
    assert doc.locked_at is not None, (
        "locked_at is NULL after dispatch_documents(), so there is no publish "
        "time to age. This is the bug under test: nothing used to write the "
        "column. Do not paper over it by assigning a timestamp here."
    )
    doc.locked_at = now() - timedelta(seconds=seconds)


def _run_recover(monkeypatch):
    """Run recover() with the broker captured instead of publishing.

    `recover()` imports `process_document` from app.tasks at the point of use
    and calls `.delay()` on it. Stubbing the module attribute is enough: the
    import happens inside the function, after monkeypatch has run.
    """
    from app import tasks, workflow

    class _Captured:
        def __init__(self):
            self.calls: list[tuple] = []

        def delay(self, *args):
            self.calls.append(args)

    stub = _Captured()
    monkeypatch.setattr(tasks, "process_document", stub)
    monkeypatch.setattr(tasks, "process_unit", stub)
    workflow.recover()
    return stub.calls


def test_recover_republishes_a_document_whose_publish_was_lost(
    stranded_document, monkeypatch
):
    """The regression: the branch never ran because locked_at was always NULL."""
    from app.db import SessionLocal

    doc_id, session_id = stranded_document

    # recover()'s cutoff is 60 seconds. Age the publish past it.
    with SessionLocal.begin() as db:
        _age_the_publish(db, doc_id, seconds=300)

    published = _run_recover(monkeypatch)

    assert (str(doc_id), str(session_id)) in published, (
        "recover() did not republish the stranded document. Its message was "
        "dropped by the broker, so nothing else will ever deliver it -- "
        "dispatch_documents() only selects 'pending' and 'retry'."
    )


def test_republish_moves_the_deadline_forward(stranded_document, monkeypatch):
    """A broker that stays down must not be hammered once per beat.

    locked_at is refreshed on each republish, so a document whose publish keeps
    failing is retried at the cutoff interval rather than on every beat. The
    original branch did write it; this pins that the fix kept that behaviour
    rather than treating locked_at as write-once.
    """
    from app.db import SessionLocal

    doc_id, session_id = stranded_document

    with SessionLocal.begin() as db:
        _age_the_publish(db, doc_id, seconds=300)
    _run_recover(monkeypatch)

    with SessionLocal() as db:
        from app.models import Document

        after_first = db.get(Document, doc_id).locked_at

    assert after_first is not None

    # Immediately re-run: the deadline was just refreshed, so the document is no
    # longer older than the 60-second cutoff and must not be published again.
    second = _run_recover(monkeypatch)
    assert (str(doc_id), str(session_id)) not in second, (
        "recover() republished a document it had only just republished -- "
        "locked_at was not refreshed, so a broker outage turns into one publish "
        "per beat per document"
    )


def test_a_document_published_recently_is_left_alone(stranded_document, monkeypatch):
    """The other half of the condition: still in flight is not stranded.

    Without this, a document published 5 seconds ago would be republished on the
    next beat and the two workers would race for the same session.
    """
    doc_id, session_id = stranded_document

    published = _run_recover(monkeypatch)

    assert (str(doc_id), str(session_id)) not in published, (
        "recover() republished a document that was handed to the broker seconds "
        "ago and is still legitimately in flight"
    )


def test_processing_document_is_not_republished(stranded_document, monkeypatch):
    """A document being worked on has a live worker; recover() must not interfere.

    It matches the `elif` branch only if its heartbeat has expired, so with a
    fresh session it falls through both arms.
    """
    from app.db import SessionLocal

    doc_id, session_id = stranded_document

    with SessionLocal.begin() as db:
        from app.models import Document

        doc = db.get(Document, doc_id, with_for_update=True)
        doc.status = "processing"

    published = _run_recover(monkeypatch)

    assert (str(doc_id), str(session_id)) not in published


def test_null_locked_at_is_not_invented_for_old_rows(stranded_document, monkeypatch):
    """Documents written before this fix are not retroactively recovered.

    A row with locked_at NULL predates the publish-time stamp, so it says
    nothing about when the publish happened. Backfilling it from
    discovered_at would be a guess, and guessing here means either republishing
    a document that is genuinely in flight or never republishing one that is not.
    The honest behaviour is to leave it and let a manual retry deal with it.
    """
    from app.db import SessionLocal

    from app.models import Document

    doc_id, session_id = stranded_document

    with SessionLocal.begin() as db:
        doc = db.get(Document, doc_id, with_for_update=True)
        doc.locked_at = None
        # Far older than any cutoff, to prove age is not what is missing.
        doc.discovered_at = db.get(Document, doc_id).discovered_at - timedelta(days=30)

    published = _run_recover(monkeypatch)

    assert (str(doc_id), str(session_id)) not in published, (
        "a row with no publish timestamp must not be republished on a guess"
    )

    with SessionLocal() as db:
        assert db.get(Document, doc_id).locked_at is None, (
            "recover() invented a publish timestamp for a row that never had one"
        )