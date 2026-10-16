"""An uploaded PDF must not start processing until the user asks it to.

`/uploads` used to be a fire button: POST the files, and `discover_documents`
plus the 15-second beat schedule began OCR on every one of them. With 300+ PDFs
from a folder there was no moment at which a user could look at what arrived and
say "that one is wrong" -- and `retry_document` only accepts failed/cancelled,
so a document that succeeded once could never be re-run at all.

The fix is a column, `documents.approved`, that `dispatch_documents()` filters
on. That makes the gate easy to break by accident: remove the filter, or add a
new caller of `_discover_under` that forgets to pass `approved=False`, and
uploads silently start running again with no visible symptom. These tests pin
both halves -- that unticked PDFs stay put, and that ticked ones move.

These tests run against the live Postgres in the compose stack and skip when it
is unreachable, so `pytest` still works on a laptop with no stack.
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


class _StubUpload:
    """The minimum `save_upload_batch` touches.

    Real `starlette.UploadFile` needs a spooled file object and a running event
    loop to construct cleanly; these tests are about how many files are
    accepted and what the code does with each one, not about multipart
    parsing, which is FastAPI's job and is covered by the route itself.
    """

    def __init__(self, filename: str, body: bytes = b"%PDF-1.4\n%stub\n"):
        self.filename = filename
        self._body = body
        self._at = 0

    async def read(self, size: int) -> bytes:
        chunk = self._body[self._at:self._at + size]
        self._at += len(chunk)
        return chunk

    async def close(self) -> None:
        return None


def _write_pdf(path: Path, name: str) -> None:
    """A file discovery will accept: %PDF- magic is all it checks."""
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"%PDF-1.4\n" + name.encode() * 32)


@pytest.fixture
def batch_row():
    """An UploadBatch plus everything created under it, removed afterwards.

    Teardown deletes through `delete_documents` so the FK order is exercised the
    same way production does it, then removes the batch. The one thing it has to
    do first is un-claim any document a test dispatched: `delete_documents`
    refuses `queued` and `processing` on purpose, so a test that dispatched
    leaves its rows behind, which then blocks the batch delete and every later
    run that reuses the table.

    Without this the fixture leaked. `uq_documents_hash` is UNIQUE on
    (source_type, file_hash), so a leaked row poisons the next run of the same
    test rather than failing cleanly.
    """
    from app.db import SessionLocal
    from app.models import UploadBatch

    batch_id = uuid.uuid4()
    with SessionLocal.begin() as db:
        db.add(UploadBatch(
            id=batch_id,
            label="test upload",
            source="files",
            relative_directory=f"test-{batch_id.hex[:8]}",
            file_count=0,
        ))
    yield batch_id

    from app.models import Document
    from app.workflow import delete_documents

    with SessionLocal.begin() as db:
        db.query(Document).filter(
            Document.upload_batch_id == batch_id,
            Document.status.in_(("queued", "processing")),
        ).update({Document.status: "pending"}, synchronize_session=False)

    with SessionLocal() as db:
        # .all() hands back one-element Row tuples, not UUIDs. delete_documents
        # calls uuid.UUID(str(i)) on whatever it is given, and str() of a Row is
        # "(UUID('...'),)" -- which raised 'badly formed hexadecimal UUID string'
        # and aborted the teardown before deleting anything.
        ids = [row[0] for row in
               db.query(Document.id).filter(Document.upload_batch_id == batch_id).all()]
    if ids:
        delete_documents(ids)

    with SessionLocal.begin() as db:
        db.query(UploadBatch).filter(UploadBatch.id == batch_id).delete()


@pytest.fixture
def make_document():
    """Insert a Document the way discovery would, and return its id."""
    from app.db import SessionLocal
    from app.models import Document

    made: list[uuid.UUID] = []

    def make(**overrides):
        # uq_documents_hash is UNIQUE (source_type, file_hash), so a shared
        # literal hash would collide across tests. One per document.
        values = dict(
            source_type="upload",
            source_path=f"/tmp/test-{uuid.uuid4().hex}.pdf",
            file_name="test.pdf",
            file_hash=uuid.uuid4().hex * 2,
            status="pending",
        )
        values.update(overrides)
        doc = Document(**values)
        with SessionLocal.begin() as db:
            db.add(doc)
            db.flush()
            made.append(doc.id)
        return doc.id

    yield make

    from sqlalchemy import delete

    from app.models import ExtractionSession, ExtractionUnit, ProcessingEvent

    # .all(), not iteration over the Query: a bare list(Query) yields Row
    # objects, and those get handed straight to IN (%s) below, where psycopg
    # cannot adapt a Row. `.in_()` needs scalars.
    #
    # Order is children first. extraction_sessions.document_id and
    # processing_events.document_id both reference documents with no cascade, so
    # deleting the document first is a ForeignKeyViolation, not a partial
    # cleanup -- the whole transaction rolls back and nothing is removed.
    with SessionLocal.begin() as db:
        session_ids = [row[0] for row in db.query(ExtractionSession.id).filter(
            ExtractionSession.document_id.in_(made)).all()]
        unit_ids = [row[0] for row in db.query(ExtractionUnit.id).filter(
            ExtractionUnit.session_id.in_(session_ids)).all()]
        db.execute(delete(ProcessingEvent).where(ProcessingEvent.document_id.in_(made)))
        if session_ids:
            db.execute(delete(ProcessingEvent).where(ProcessingEvent.session_id.in_(session_ids)))
        if unit_ids:
            db.execute(delete(ExtractionUnit).where(ExtractionUnit.id.in_(unit_ids)))
        if session_ids:
            db.execute(delete(ExtractionSession).where(ExtractionSession.id.in_(session_ids)))
        db.execute(delete(Document).where(Document.id.in_(made)))


@pytest.fixture
def swallowed_broker(monkeypatch):
    """A broker that accepts every message and delivers none."""
    from app import tasks

    class _Swallowed:
        def __init__(self):
            self.calls: list[tuple] = []

        def delay(self, *args):
            self.calls.append(args)
            return None

    broker = _Swallowed()
    monkeypatch.setattr(tasks, "process_document", broker)
    monkeypatch.setattr(tasks, "process_unit", broker)
    return broker


# ── The upload file-count cap is gone ──────────────────────────────────────

def test_upload_accepts_more_files_than_the_old_500_cap(tmp_path, monkeypatch):
    """A 520-PDF folder must save.

    `save_upload_batch` used to reject anything over MAX_UPLOAD_FILES with a
    message and delete nothing -- so a real roll of 1000 scanned PDFs could not
    be uploaded at all, and the operator had no way to find out what the folder
    held. The byte limits remain; the count limit was the one that blocked the
    actual use case.
    """
    import asyncio

    from app import config
    from app.uploads import save_upload_batch

    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    files = [_StubUpload(f"roll_{i:04d}.pdf") for i in range(520)]

    summary = asyncio.run(save_upload_batch(files, "big-upload"))

    assert summary.file_count == 520
    saved = list((tmp_path / summary.relative_directory).glob("*.pdf"))
    assert len(saved) == 520


def test_two_uploads_of_the_same_name_get_different_directories(tmp_path, monkeypatch):
    """Directory names are label + timestamp + a short uuid.

    The timestamp alone is not enough -- two uploads in the same second with the
    same label would collide, and the second would fail on the existing-
    directory check or merge into the first. The uuid suffix is what keeps
    them apart.
    """
    import asyncio

    from app import config
    from app.uploads import save_upload_batch

    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)

    first = asyncio.run(save_upload_batch([_StubUpload("a.pdf")], "Delhi North"))
    second = asyncio.run(save_upload_batch([_StubUpload("b.pdf")], "Delhi North"))

    assert first.relative_directory != second.relative_directory
    assert (tmp_path / first.relative_directory / "a.pdf").exists()
    assert (tmp_path / second.relative_directory / "b.pdf").exists()


# ── Discovery registers, but does not approve ─────────────────────────────

def test_discovered_uploads_are_numbered_and_left_unapproved(tmp_path, monkeypatch, batch_row):
    """An upload must appear as one contiguous 1..N run and start nothing.

    Both halves matter. Numbering by source_path is what puts a file back in
    the same slot on a re-scan instead of shuffling the table under a user who
    has already ticked rows; `approved=False` is the gate.
    """
    from app import config
    from app.db import SessionLocal
    from app.models import Document, UploadBatch
    from app.workflow import discover_upload_batch

    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    directory = tmp_path / f"test-{batch_row.hex[:8]}"
    # Written out of order on purpose: numbering must follow the path, not
    # the order the filesystem happens to hand them back.
    for name in ("c.pdf", "a.pdf", "b.pdf"):
        _write_pdf(directory, name)

    with SessionLocal.begin() as db:
        db.get(UploadBatch, batch_row).relative_directory = directory.name

    assert discover_upload_batch(batch_row) == 3

    from sqlalchemy import select

    with SessionLocal() as db:
        docs = db.scalars(
            select(Document).where(Document.upload_batch_id == batch_row)
            .order_by(Document.batch_index)
        ).all()
        assert [d.file_name for d in docs] == ["a.pdf", "b.pdf", "c.pdf"]
        assert [d.batch_index for d in docs] == [1, 2, 3]
        assert all(d.approved is False for d in docs), (
            "an uploaded PDF was approved at discovery time -- it would start "
            "OCR on the next beat tick, before the user could see it"
        )


def test_scan_folder_documents_are_approved_by_default(tmp_path, monkeypatch):
    """`_discover_under` defaults `approved=True`.

    Defaulting it the other way round would strand every registered scan folder
    the moment the column was added: those documents would be discovered by the
    hourly beat and then never run, with no error anywhere. This test exists to
    make that default deliberate rather than accidental.

    Two things make this test able to poison itself, both fixed here rather than
    worked around:

    - The bytes are written per-run, not the shared deterministic stub. A scan
      root's source identity is `root:<id>:<relative path>#<digest>`, so with
      fixed bytes a row that outlives one run collides with the next on
      uq_documents_source and `on_conflict_do_nothing` swallows the insert --
      `_discover_under` then returns 0 and the test fails on a leaked row from
      a previous run, not on anything it just asserted.

    - Cleanup is in a `finally`. This is the one test here that creates an
      *approved* document, so if the assertion fails the row stays dispatchable
      and the 15-second beat picks it up and runs OCR against it. That is a real
      side effect on the running stack, not just a dirty test database.
    """
    from app import config
    from app.workflow import _discover_under

    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    root = tmp_path / "scanned"
    root.mkdir(parents=True, exist_ok=True)
    (root / "roll_001.pdf").write_bytes(b"%PDF-1.4\n" + uuid.uuid4().hex.encode() * 32)

    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import delete_documents

    try:
        assert _discover_under(root, "scan root", scan_root_id=None) == 1

        with SessionLocal() as db:
            doc = db.query(Document).filter(
                Document.source_path.like(f"{root}%")).first()
            assert doc is not None, "scan-folder discovery registered nothing"
            assert doc.approved is True, (
                "a scan-folder PDF was left unapproved and would never be processed"
            )
    finally:
        # Through delete_documents, not a bare DELETE: discovery writes a
        # DOCUMENT_DISCOVERED row into processing_events, and nothing in this
        # schema cascades, so deleting the document directly is a
        # ForeignKeyViolation that rolls the whole cleanup back.
        with SessionLocal() as db:
            ids = [row[0] for row in db.query(Document.id).filter(
                Document.source_path.like(f"{root}%")).all()]
        if ids:
            delete_documents(ids)


# ── The gate ───────────────────────────────────────────────────────────────

def test_dispatch_ignores_an_unapproved_document(batch_row, make_document, swallowed_broker):
    """The whole point of the column.

    `dispatch_documents` runs every 15 seconds off the beat schedule. Without
    the `approved` filter it would pick up every freshly-uploaded PDF within a
    tick of the file landing on disk, and the /uploads page would be a
    formality rather than a staging area.
    """
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import dispatch_documents

    doc_id = make_document(upload_batch_id=batch_row, approved=False, priority=1)

    dispatch_documents(limit=200)

    assert not any(call and str(call[0]) == str(doc_id) for call in swallowed_broker.calls), (
        "an unapproved document was published to the broker"
    )
    with SessionLocal() as db:
        assert db.get(Document, doc_id).status == "pending"


def test_dispatch_takes_an_approved_document(batch_row, make_document, swallowed_broker):
    """The other half: once ticked, the same beat must pick it up, or the
    Extract button would silently do nothing."""
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import dispatch_documents

    doc_id = make_document(upload_batch_id=batch_row, approved=True, priority=1)

    dispatch_documents(limit=200)

    assert any(call and str(call[0]) == str(doc_id) for call in swallowed_broker.calls), (
        "an approved document was not dispatched"
    )
    with SessionLocal() as db:
        assert db.get(Document, doc_id).status == "queued"


# ── What Extract does per status ───────────────────────────────────────────

def test_extract_approves_a_pending_document(batch_row, make_document):
    """The ordinary first run: approving is the only change needed."""
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import select_documents_for_extraction

    doc_id = make_document(upload_batch_id=batch_row, status="pending", approved=False)
    accepted, skipped = select_documents_for_extraction([doc_id])

    assert (accepted, skipped) == (1, 0)
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        assert doc.approved is True
        assert doc.status == "pending"


def test_extract_re_extracts_a_completed_document(batch_row, make_document):
    """A document that already succeeded has to be runnable again.

    `retry_document` refuses anything outside failed/cancelled, so before this
    existed a wrong-but-completed extraction was permanent: the user could
    see it was wrong and had no way to fix it without deleting the row.
    """
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import select_documents_for_extraction

    doc_id = make_document(upload_batch_id=batch_row, status="completed",
                           approved=True, attempt_count=1, max_attempts=3)
    accepted, _ = select_documents_for_extraction([doc_id])

    assert accepted == 1
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        assert doc.status == "pending", "a completed document was not re-queued"
        assert doc.processing_completed_at is None


def test_extract_lifts_the_attempt_ceiling_for_a_failed_document(batch_row, make_document):
    """`dispatch_documents` re-fails anything where attempt_count >=
    max_attempts, so re-queueing a failed document without raising the ceiling
    would flip it straight back to failed on the next tick -- a button that
    appears to work and does nothing."""
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import select_documents_for_extraction

    doc_id = make_document(upload_batch_id=batch_row, status="failed",
                           approved=True, attempt_count=3, max_attempts=3)
    accepted, _ = select_documents_for_extraction([doc_id])

    assert accepted == 1
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        assert doc.max_attempts > doc.attempt_count
        assert doc.status == "pending"


def test_extract_refuses_a_document_already_running(batch_row, make_document):
    """A worker holds a queued or processing row together with its session and
    an in-flight publish. Re-queuing it would put two jobs on one PDF."""
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import select_documents_for_extraction

    doc_id = make_document(upload_batch_id=batch_row, status="processing",
                           approved=True, attempt_count=1)
    accepted, skipped = select_documents_for_extraction([doc_id])

    assert (accepted, skipped) == (0, 1)
    with SessionLocal() as db:
        assert db.get(Document, doc_id).status == "processing"


def test_extract_counts_an_unknown_id_as_skipped(batch_row, make_document):
    """A stale page -- a PDF deleted in another tab -- must not fail the whole
    selection or abort the documents that do exist."""
    from app.workflow import select_documents_for_extraction

    good = make_document(upload_batch_id=batch_row, status="pending", approved=False)
    accepted, skipped = select_documents_for_extraction([good, uuid.uuid4()])

    assert accepted == 1
    assert skipped == 1


# ── Deletion ───────────────────────────────────────────────────────────────

def test_delete_removes_the_row_and_its_session(batch_row, make_document, swallowed_broker):
    """Deleting has to clear the whole chain.

    No foreign key in this schema cascades, so the children go first. Doing it
    in the wrong order does not half-delete -- Postgres rejects the statement
    and the transaction rolls back, leaving the document exactly as it was.

    The document is left `pending` rather than dispatched first. A dispatch is
    what this test used to do to get a session to clean up, and it made the
    document `queued` -- which `delete_documents` refuses on purpose, because a
    worker holds that row and an in-flight publish. The session is written
    directly instead, which is what dispatch would have produced, and it keeps
    the subject here deletion order rather than dispatch's side effect.
    """
    from app.db import SessionLocal
    from app.models import Document, ExtractionSession
    from app.workflow import delete_documents

    doc_id = make_document(upload_batch_id=batch_row, status="pending",
                           approved=True, priority=1)

    session_id = uuid.uuid4()
    with SessionLocal.begin() as db:
        # attempt_number is NOT NULL and status is CHECK-constrained to
        # created/queued/processing/completed/partial/failed/abandoned, so both
        # have to be given explicitly on a hand-built row.
        db.add(ExtractionSession(id=session_id, document_id=doc_id, status="processing",
                                 attempt_number=1))
        db.get(Document, doc_id).current_session_id = session_id

    deleted, skipped = delete_documents([doc_id])

    assert (deleted, skipped) == (1, 0)
    with SessionLocal() as db:
        assert db.get(Document, doc_id) is None
        assert db.get(ExtractionSession, session_id) is None


def test_delete_removes_the_file_from_disk(tmp_path, monkeypatch, batch_row, make_document):
    """A deleted PDF that stays on disk is rediscovered by the next scan, so
    the user watches a row they deleted reappear."""
    from app import config
    from app.workflow import delete_documents

    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    directory = tmp_path / f"test-{batch_row.hex[:8]}"
    _write_pdf(directory, "gone.pdf")
    doc_id = make_document(upload_batch_id=batch_row, status="pending",
                           source_path=str(directory / "gone.pdf"))

    deleted, _ = delete_documents([doc_id])

    assert deleted == 1
    assert not (directory / "gone.pdf").exists()


def test_delete_refuses_a_processing_document(batch_row, make_document):
    """Deleting under a live worker leaves its publish pointing at a row that
    no longer exists, and the job fails on a document it can no longer read."""
    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import delete_documents

    doc_id = make_document(upload_batch_id=batch_row, status="processing",
                           approved=True, attempt_count=1)
    deleted, skipped = delete_documents([doc_id])

    assert (deleted, skipped) == (0, 1)
    with SessionLocal() as db:
        assert db.get(Document, doc_id) is not None