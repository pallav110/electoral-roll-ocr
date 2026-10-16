"""The queue must not grow without bound, and nothing live may be republished.

A 27-page roll once reported 7 hr 45 min while its own units had done about
5 minutes of work. Two things caused it, and both are pinned here:

  * dispatch_units published up to 100 units per call, every 10 seconds, into a
    queue one worker drained at a time -- so the broker's backlog grew faster
    than it could be consumed, and each tick pushed real work further back.

  * recover() republished every unit "queued" longer than a hardcoded 60 seconds.
    With any real backlog that condition is true for everything, so the
    60-second beat doubled the entire queue on every tick, forever.

The second is the more dangerous of the two, because republishing live work does
not merely waste time: it puts duplicates into a queue, and every duplicate is
another OCR call competing for the same cores.
"""
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import delete, func, select

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Older than any row in the live database, so a fixture row always sorts ahead of
# it. Both dispatchers order by a timestamp and select across every session
# rather than one, so without this a test's own units would queue behind the real
# backlog and the window would fill with someone else's work.
_OLDEST = datetime(2000, 1, 1, tzinfo=timezone.utc)

# The real _in_flight_units, bound at import time -- before any fixture has had
# the chance to stub it. One test has to compare the stub against the query it
# stands in for, and reading the attribute off the module at that point would
# just hand back the stub.
from app.workflow import _in_flight_units as _REAL_IN_FLIGHT_UNITS  # noqa: E402


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


class _Captured:
    """Stands in for a Celery task. Records what would have been published.

    `fail_on` names the ids whose publish should raise, which is how the
    "message never arrived" case is modelled. Failing a publish is the normal
    outcome when a broker is briefly unreachable, and the row must survive it.
    Matched against every argument rather than the last one, because process_unit
    and process_document do not agree on position -- process_unit ends with the
    unit id, process_document with the session id.

    A failed publish is recorded in `calls` *before* the raise, unlike the real
    Celery client, which fails before the broker ever sees the message. So
    `calls` here means "publish attempted", not "published", and `published` is
    the honest count of what a broker would actually hold. Asserting the naive
    version -- that a failing publish records nothing -- is how the first draft
    of the publish-failure test came to depend on dispatch_units clearing the
    window between passes instead of on the behaviour it was written for.
    """

    def __init__(self, fail_on=()):
        self.calls: list[tuple] = []
        # "all" rather than a set of ids, because the fixture needs the broker
        # before the units exist and their ids are only known afterwards.
        self.fail_all = fail_on == "all"
        self.fail_on = set() if self.fail_all else set(fail_on)

    @property
    def published(self) -> list[tuple]:
        """Attempts that did not fail -- the ones a real broker would hold."""
        return [args for args in self.calls if not self._should_fail(args)]

    def _should_fail(self, args) -> bool:
        return self.fail_all or any(arg in self.fail_on for arg in args)

    def delay(self, *args):
        self.calls.append(args)
        if self._should_fail(args):
            raise RuntimeError("broker is unreachable")
        return None


def _statuses(session_id) -> dict:
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with SessionLocal() as db:
        return dict(
            db.execute(
                select(ExtractionUnit.status, func.count())
                .where(ExtractionUnit.session_id == session_id)
                .group_by(ExtractionUnit.status)
            ).all()
        )


def _units(session_id):
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with SessionLocal() as db:
        return db.scalars(
            select(ExtractionUnit)
            .where(ExtractionUnit.session_id == session_id)
            .order_by(ExtractionUnit.unit_number)
        ).all()


def _live_in_flight() -> int:
    """Units already queued or processing in the shared database.

    Every test stubs the count instead -- see the fixture. This exists so one
    test can compare a stub against the real query and prove the two agree.
    """
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with SessionLocal() as db:
        return db.scalar(
            select(func.count())
            .select_from(ExtractionUnit)
            .where(ExtractionUnit.status.in_(["queued", "processing"]))
        ) or 0


@pytest.fixture
def unit_batch(monkeypatch):
    """Builds a processing session with N pending units, plus a stubbed broker.

    Built by hand rather than through dispatch, because the subject here is what
    dispatch does with units that already exist -- the state a large upload
    leaves behind. uq_documents_hash is UNIQUE (source_type, file_hash), so each
    test generates its own hash; see test_document_recovery.py:90-92.

    `fail_on` names unit ids whose publish should raise. It has to reach the stub
    at construction because the in-flight count is itself stubbed to a constant:
    a fixed 0 tells the second dispatch pass that there is still room, so a
    publish-failure test can only see the failure on the first pass.

    `make_session=False` is for the document-level test. dispatch_documents creates
    its own ExtractionSession, and extraction_sessions is UNIQUE on
    (document_id, attempt_number) -- so a pre-made session of attempt 1 collides
    with the one it tries to insert.

    A `with` block rather than a `for` loop, because a loop re-enters on each
    pass: the rows are created once when the generator is first advanced, so a
    loop that ran it twice would tear down one batch while another sat in the
    table. That is not hypothetical -- it is how the fixture leaked for a while.

    The patches run on their own MonkeyPatch rather than the test's. Both reasons
    matter. Sharing it would mean this fixture's teardown undoes whatever the
    test patched before entering, which is how a stub set up once outside the
    block silently vanished on the second entry; and monkeypatch.undo() is
    all-or-nothing, so undoing it to reach the real _in_flight_units (as one test
    does) would also drop the broker stub.
    """
    from app import config, tasks, workflow
    from app.db import SessionLocal
    from app.models import Document, ExtractionSession, ExtractionUnit, ProcessingEvent

    @contextmanager
    def build(count: int, room_for=2, in_flight=0, document_status="processing",
              created_at=None, fail_on=(), make_session=True):
        scope = MonkeyPatch()
        broker = _Captured(fail_on=fail_on)
        scope.setattr(tasks, "process_unit", broker)

        doc_id, session_id = uuid.uuid4(), uuid.uuid4()
        with SessionLocal.begin() as db:
            db.add(
                Document(
                    id=doc_id,
                    source_type="upload",
                    source_path=f"/tmp/test-window-{doc_id}.pdf",
                    file_name="test-window.pdf",
                    file_hash=uuid.uuid4().hex * 2,
                    status=document_status,
                    approved=True,
                    current_session_id=session_id if make_session else None,
                    # dispatch_documents orders by priority, then discovered_at,
                    # and the shared table holds real pending documents discovered
                    # hours ago. Priority 0 sorts ahead of all of them, so this
                    # test dispatches its own document rather than a neighbour's.
                    priority=0,
                )
            )
            if make_session:
                db.add(
                    ExtractionSession(
                        id=session_id,
                        document_id=doc_id,
                        attempt_number=1,
                        status="processing",
                    )
                )
            for number in range(1, count + 1):
                db.add(
                    ExtractionUnit(
                        session_id=session_id,
                        unit_number=number,
                        page_from=1 + (number - 1) * 3,
                        page_to=3 + (number - 1) * 3,
                        status="pending",
                        # dispatch_units orders by created_at and selects across
                        # every session, so the same race applies: without this
                        # the units under test would sit behind the real backlog
                        # and the window would fill with someone else's work.
                        created_at=created_at or _OLDEST,
                    )
                )

        # The window is global, not per-session -- that is the whole point, since
        # what is bounded is the broker's backlog. So a test has to say what the
        # rest of the world is holding. It says so here by stubbing the count,
        # rather than measuring the live table and hoping nothing moves in
        # between: the beat dispatches every ten seconds against the same rows,
        # so a measured baseline would race it and fail intermittently rather
        # than honestly.
        scope.setattr(workflow, "_in_flight_units", lambda db: in_flight)
        scope.setattr(config, "OCR_MAX_INFLIGHT_UNITS", in_flight + room_for)

        unit_ids = {str(u) for u in _units(session_id)}
        try:
            yield broker, doc_id, session_id, unit_ids
        finally:
            # Restores every patch above, including the process_unit broker stub.
            # In a finally, because a test that fails mid-body would otherwise
            # leak this batch's stub into every test after it.
            scope.undo()

            with SessionLocal.begin() as db:
                # Keyed off the document, not session_id: dispatch_documents
                # mints its own session with its own id, so deleting by the one
                # this fixture created would leave the session it made behind --
                # which still holds the document and still trips the UNIQUE
                # (document_id, attempt_number) constraint on the next run.
                #
                # Events first: processing_events references both the session and
                # the unit, so deleting units first violates the FK. Same order as
                # test_document_recovery.py:134-138.
                sessions = select(ExtractionSession.id).where(ExtractionSession.document_id == doc_id)
                db.execute(delete(ProcessingEvent).where(ProcessingEvent.document_id == doc_id))
                db.execute(delete(ExtractionUnit).where(ExtractionUnit.session_id.in_(sessions)))
                db.execute(delete(ExtractionSession).where(ExtractionSession.document_id == doc_id))
                db.execute(delete(Document).where(Document.id == doc_id))

    return build


def test_dispatch_never_exceeds_the_in_flight_window(unit_batch):
    """The core fix: 5 pending units, a window of 2, so exactly 2 are published.

    Publishing all 5 was what let the queue outrun the worker. The remaining 3
    stay pending, which is not a lost message: dispatch_units runs every 10
    seconds and picks them up as the window drains.
    """
    from app import workflow

    with unit_batch(5, room_for=2) as (broker, doc_id, session_id, unit_ids):
        assert workflow.dispatch_units() == 2
        assert len(broker.calls) == 2

        counts = _statuses(session_id)
        assert counts.get("queued") == 2, f"expected 2 queued, got {counts}"
        assert counts.get("pending") == 3, f"expected 3 still pending, got {counts}"


def test_dispatch_publishes_nothing_when_the_window_is_full(unit_batch):
    """A full window must produce zero publishes, not a refill attempt.

    The old code had no concept of a full window, so it kept publishing into a
    queue that was already too deep.
    """
    from app import workflow

    with unit_batch(3, room_for=0) as (broker, doc_id, session_id, unit_ids):
        assert workflow.dispatch_units() == 0
        assert broker.calls == [], "published into a full window"
        assert _statuses(session_id).get("pending") == 3, "nothing should have moved"


def test_a_unit_whose_publish_fails_returns_to_pending(unit_batch):
    """A failed publish must not leave the row "queued" with no message.

    That state is invisible to everything: dispatch_units selects only "pending"
    and "retry", and recovery's age-based republish is gone. The unit would be
    stranded for good, so the revert has to happen where the publish failed.

    Every publish fails here, on the first pass. Two passes would be the wrong
    shape: the in-flight count is stubbed to a constant, so the second pass would
    still believe the window has room and would queue the units all over again,
    testing the stub rather than the revert.
    """
    from app import workflow

    with unit_batch(3, room_for=2, fail_on="all") as (broker, doc_id, session_id, unit_ids):
        dispatched = workflow.dispatch_units()

        assert dispatched == 2, "the first pass should have filled the window"
        assert broker.published == [], "the broker was supposed to reject every publish"
        counts = _statuses(session_id)
        assert counts.get("pending") == 3, (
            f"a unit whose publish failed is not pending, so nothing will ever "
            f"republish it: {counts}"
        )
        assert counts.get("queued") is None, (
            f"a row left queued with no message behind it is stranded for good: {counts}"
        )


def test_recover_does_not_republish_a_queued_unit_that_is_merely_waiting(unit_batch):
    """The regression test for the republish storm.

    A unit queued inside the window is waiting behind the unit ahead of it,
    which is correct behaviour. Under any real backlog it is always older than
    the old hardcoded 60 seconds, so recover() republished every one of them on
    every 60-second beat. This asserts the storm is gone: age alone no longer
    republishes.
    """
    from app import tasks, workflow
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with unit_batch(2, room_for=0) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            for unit in db.scalars(
                select(ExtractionUnit).where(ExtractionUnit.session_id == session_id)
            ).all():
                unit.status = "queued"
                unit.queued_at = workflow.now() - timedelta(seconds=300)

        republisher = _Captured()
        original = tasks.process_unit
        tasks.process_unit = republisher
        try:
            workflow.recover()
        finally:
            tasks.process_unit = original

        assert republisher.calls == [], (
            "recover() republished units that were only waiting. This is the "
            "storm: with a real backlog every queued unit is older than the "
            "cutoff, so the beat doubled the whole queue on every tick."
        )


def test_dispatch_reclaims_a_queued_unit_whose_message_never_arrived(
    unit_batch, monkeypatch
):
    """The orphaned case: "queued" far past the recovery threshold.

    This is the state a worker restart leaves behind -- rows queued with
    started_at NULL and no message anywhere. They have to come back to pending,
    or they are stranded permanently.

    Reclaiming happens before the window is counted, so a reclaimed unit is
    republished in the same pass rather than waiting for the next beat.
    """
    from app import config, workflow
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    monkeypatch.setattr(config, "QUEUED_RECOVERY_SECONDS", 60)
    with unit_batch(1, room_for=2) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            unit = db.scalars(
                select(ExtractionUnit).where(ExtractionUnit.session_id == session_id)
            ).one()
            unit.status = "queued"
            unit.queued_at = workflow.now() - timedelta(seconds=600)

        workflow.dispatch_units()

        assert broker.calls, "the orphaned unit was not republished"
        assert _statuses(session_id).get("queued") == 1, (
            "reclaimed and republished in the same pass, so it should be queued again"
        )


def test_a_queued_unit_inside_the_window_is_not_reclaimed(unit_batch, monkeypatch):
    """The other half of the reclaim condition: waiting is not stranded.

    Without this, a unit legitimately queued behind a long OCR call would be
    reclaimed and republished mid-flight, duplicating live work.

    The window is stated as an absolute count, so the reclaim threshold can be
    made small enough that a freshly queued unit is *inside* it -- the opposite
    condition from the test above, on the same code path.
    """
    from app import config, workflow

    monkeypatch.setattr(config, "QUEUED_RECOVERY_SECONDS", 600)
    monkeypatch.setattr(config, "OCR_MAX_INFLIGHT_UNITS", 10)
    with unit_batch(2, room_for=10) as (broker, doc_id, session_id, unit_ids):
        assert workflow.dispatch_units() == 2
        assert len(broker.calls) == 2

        # Now genuinely windowed in: two queued, room for none. If the threshold
        # were short enough to reclaim these, the window would never hold.
        monkeypatch.setattr(config, "OCR_MAX_INFLIGHT_UNITS", 2)
        broker.calls.clear()
        workflow.dispatch_units()

        assert broker.calls == [], (
            "a unit waiting inside the window was republished. A unit can sit "
            "queued for as long as the unit ahead of it runs."
        )


def test_recover_still_retries_a_unit_whose_heartbeat_expired(unit_batch):
    """The branch that stays: a real worker death, proven by an expired heartbeat.

    Removing the age-based republish must not remove recovery itself. A unit
    processing with a heartbeat older than UNIT_STALE_SECONDS is a worker that
    is gone, and nothing else will ever move it.
    """
    from app import config, workflow
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with unit_batch(1, room_for=0) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            unit = db.scalars(
                select(ExtractionUnit).where(ExtractionUnit.session_id == session_id)
            ).one()
            unit.status = "processing"
            unit.worker_id = "dead-worker"
            unit.started_at = workflow.now() - timedelta(
                seconds=config.UNIT_STALE_SECONDS + 120
            )
            unit.heartbeat_at = unit.started_at

        workflow.recover()

        assert _statuses(session_id).get("retry") == 1, (
            "a unit with a dead heartbeat was not returned to retry. Recovery was "
            "over-narrowed: it must still catch worker death."
        )


def test_recover_does_not_abandon_a_stale_session_with_pending_units(
    unit_batch, monkeypatch
):
    """Waiting behind sequential OCR is not evidence of a lost worker.

    A document session gets its initial heartbeat when its units are created.
    With a 20-document window and a one-unit OCR lane, later sessions can wait
    longer than DOCUMENT_STALE_SECONDS before their first unit starts. Recovery
    must leave those schedulable pending units alone instead of consuming a
    document attempt every recovery tick.
    """
    from app import config, workflow
    from app.db import SessionLocal
    from app.models import Document, ExtractionSession

    monkeypatch.setattr(config, "DOCUMENT_STALE_SECONDS", 60)
    with unit_batch(3, room_for=0) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            session = db.get(ExtractionSession, session_id, with_for_update=True)
            session.pages_total = 9
            session.started_at = workflow.now() - timedelta(seconds=600)
            session.heartbeat_at = session.started_at

        workflow.recover()

        with SessionLocal() as db:
            assert db.get(Document, doc_id).status == "processing"
            assert db.get(ExtractionSession, session_id).status == "processing"
        assert _statuses(session_id).get("pending") == 3


def test_recover_reaps_stale_session_with_pages_but_no_units(
    unit_batch, monkeypatch
):
    """Keep recovery for the genuinely broken post-page-count state."""
    from app import config, workflow
    from app.db import SessionLocal
    from app.models import Document, ExtractionSession

    monkeypatch.setattr(config, "DOCUMENT_STALE_SECONDS", 60)
    with unit_batch(0, room_for=0) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            session = db.get(ExtractionSession, session_id, with_for_update=True)
            session.pages_total = 9
            session.started_at = workflow.now() - timedelta(seconds=600)
            session.heartbeat_at = session.started_at

        workflow.recover()

        with SessionLocal() as db:
            assert db.get(Document, doc_id).status == "retry"
            assert db.get(ExtractionSession, session_id).status == "abandoned"


def test_the_in_flight_count_is_global_and_live(unit_batch):
    """The bound counts the whole table, and counts exactly the right rows.

    Every other test here stubs this count, which is what makes it worth pinning
    once against the real query: if it ever gained a session filter, the stubbed
    tests would keep passing while the bound quietly stopped bounding.

    The unit_batch rows are created "pending", which is the point of the negative
    assertion -- pending work is the backlog the window exists to hold back, so
    counting it would make the window permanently full and nothing would ever
    dispatch. Setting the fixture's rows to queued and watching the count move by
    exactly two proves the count is live over the whole table rather than
    hard-coded or session-scoped: both bugs would leave `real` unchanged here.
    """
    from app import workflow
    from app.db import SessionLocal
    from app.models import ExtractionUnit

    with unit_batch(2, room_for=0) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal() as db:
            real = _REAL_IN_FLIGHT_UNITS(db)
            total, pending = db.execute(
                select(
                    func.count(),
                    func.count().filter(ExtractionUnit.status == "pending"),
                ).select_from(ExtractionUnit)
            ).one()

        assert real == _live_in_flight(), (
            "the count no longer agrees with a direct query over queued and "
            "processing units; the stub every other test relies on would be "
            "describing something dispatch_units no longer does"
        )
        # Stated against the table rather than as "is zero", so the assertion is
        # about which rows are counted and holds whatever else the suite has left
        # behind. Pending units are the backlog the window exists to hold back:
        # counting them would fill the window permanently and dispatch nothing
        # ever again.
        assert real == total - pending, (
            f"the count does not equal the queued+processing rows ({real} vs "
            f"{total - pending} of {total}). Pending units are being counted as "
            "in flight."
        )

        with SessionLocal.begin() as db:
            for unit in db.scalars(
                select(ExtractionUnit).where(ExtractionUnit.session_id == session_id)
            ).all():
                unit.status = "queued"
                unit.queued_at = workflow.now()

        with SessionLocal() as db:
            assert _REAL_IN_FLIGHT_UNITS(db) == real + 2, (
                "queued units in this session did not register. The count is "
                "either session-scoped or not live -- and the whole window is "
                "only as trustworthy as this number."
            )


def test_dispatch_documents_is_bounded_by_document_max_inflight(unit_batch, monkeypatch):
    """The same bound at the document level.

    Without it, one beat tick claimed up to 100 documents and each one created
    its session and unit rows immediately -- so the /documents page showed 100
    documents as "processing" while none of them had a unit near a worker. This
    is the same unbounded-publish bug one level up.

    No session is pre-made here (make_session=False): dispatch_documents creates
    one per document as part of the transition, and extraction_sessions is UNIQUE
    on (document_id, attempt_number) -- a pre-made attempt 1 collides with it.
    """
    from app import config, tasks, workflow

    # Bound already reached, so there is no room for the fixture's document.
    monkeypatch.setattr(workflow, "_in_flight_documents", lambda db: 20)
    monkeypatch.setattr(config, "DOCUMENT_MAX_INFLIGHT", 20)

    # Records every process_document publish, whether or not it fails -- so a
    # failure to publish at document level is caught here rather than silently
    # becoming an empty broker.calls that reads like "correctly held back".
    published = []
    monkeypatch.setattr(
        tasks, "process_document",
        type("Broker", (), {"delay": lambda _self, *a: published.append(a) or None})(),
    )

    with unit_batch(0, room_for=0, document_status="pending",
                    make_session=False) as (broker, doc_id, session_id, unit_ids):
        assert workflow.dispatch_documents() == 0, (
            "dispatched a document with the in-flight bound already reached"
        )
        assert published == [], "published into a full window"

    # Room for exactly one, and it is dispatched. The stubbed publisher means the
    # row is left queued with a session whose units do not exist yet, which is
    # what a broker failure looks like from here -- and recover() is what collects
    # it, not this test.
    monkeypatch.setattr(workflow, "_in_flight_documents", lambda db: 19)
    with unit_batch(0, room_for=0, document_status="pending",
                    make_session=False) as (broker, doc_id, session_id, unit_ids):
        assert workflow.dispatch_documents() == 1, (
            "room was available and the document was not dispatched"
        )
        assert len(published) == 1, (
            f"expected exactly one publish for the one document claimed, got {published}"
        )
        assert published[0][0] == str(doc_id), (
            "the publish names a document other than the one that was claimed"
        )


def test_dispatch_uses_session_history_when_attempt_count_is_stale(unit_batch, monkeypatch):
    """An abandoned attempt must not make every later dispatch roll back.

    Older recovery/repair paths could leave a retry document at attempt_count=0
    while attempt 1 already existed. The next session must be attempt 2, not a
    duplicate attempt 1 that violates the unique constraint.
    """
    from app import config, tasks, workflow
    from app.db import SessionLocal
    from app.models import Document, ExtractionSession

    monkeypatch.setattr(workflow, "_in_flight_documents", lambda db: 0)
    monkeypatch.setattr(config, "DOCUMENT_MAX_INFLIGHT", 20)
    published = []
    monkeypatch.setattr(
        tasks,
        "process_document",
        type("Broker", (), {"delay": lambda _self, *a: published.append(a) or None})(),
    )

    with unit_batch(0, room_for=0, document_status="retry",
                    make_session=False) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            doc = db.get(Document, doc_id)
            doc.attempt_count = 0
            doc.max_attempts = 3
            doc.next_retry_at = workflow.now()
            db.add(ExtractionSession(
                document_id=doc_id,
                attempt_number=1,
                status="abandoned",
            ))

        assert workflow.dispatch_documents() == 1
        assert len(published) == 1

        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            attempts = db.scalars(
                select(ExtractionSession.attempt_number)
                .where(ExtractionSession.document_id == doc_id)
                .order_by(ExtractionSession.attempt_number)
            ).all()
            assert doc.attempt_count == 2
            assert attempts == [1, 2]


def test_dispatch_skips_bytes_that_already_completed(unit_batch, monkeypatch):
    """A scoped upload id must not defeat global content-hash deduplication."""
    from app import config, tasks, workflow
    from app.db import SessionLocal
    from app.models import Document

    monkeypatch.setattr(workflow, "_in_flight_documents", lambda db: 0)
    monkeypatch.setattr(config, "DOCUMENT_MAX_INFLIGHT", 20)
    published = []
    monkeypatch.setattr(
        tasks,
        "process_document",
        type("Broker", (), {"delay": lambda _self, *a: published.append(a) or None})(),
    )

    canonical_id = uuid.uuid4()
    with unit_batch(0, room_for=0, document_status="pending",
                    make_session=False) as (broker, doc_id, session_id, unit_ids):
        with SessionLocal.begin() as db:
            candidate = db.get(Document, doc_id)
            db.add(Document(
                id=canonical_id,
                source_type="upload",
                source_document_id=f"completed:{canonical_id}",
                source_path=f"/tmp/completed-{canonical_id}.pdf",
                file_name="completed.pdf",
                file_hash=candidate.file_hash,
                status="completed",
                approved=True,
            ))

        try:
            assert workflow.dispatch_documents() == 0
            assert published == []
            with SessionLocal() as db:
                duplicate = db.get(Document, doc_id)
                assert duplicate.status == "cancelled"
                assert duplicate.last_error_code == "DUPLICATE_CONTENT"
                assert str(canonical_id) in duplicate.last_error
        finally:
            with SessionLocal.begin() as db:
                db.execute(delete(Document).where(Document.id == canonical_id))
