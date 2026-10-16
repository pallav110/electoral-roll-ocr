"""Audit or safely reconcile the current named Celery queues.

A queue is easy to get wrong in a way no log line shows: rows in the database
say what *should* run, messages in Redis say what the broker is *offering*, and
the two drift apart silently. This prints both and the difference between them.

Dry-run is the default. Apply mode is intentionally strict: the caller must
confirm that Beat and both workers are stopped, no unit may be processing, and
no document-setup message may be waiting. PostgreSQL remains the source of
truth; queued current-session units return to pending before the OCR queue is
cleared, so the bounded dispatcher can publish them again.

    python scripts/reconcile_celery_queue.py
    python scripts/reconcile_celery_queue.py --apply --confirm-workers-stopped
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, or_, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Document, ExtractionSession, ExtractionUnit  # noqa: E402

QUEUES = ("control", "ingest", "ocr")


def broker_depths() -> dict[str, int]:
    """Messages waiting per queue. A failure here is reported, not raised."""
    try:
        import redis
    except ImportError:
        return {}
    try:
        client = redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=5)
        return {name: int(client.llen(name)) for name in QUEUES}
    except Exception as exc:
        print(f"  (could not reach the broker: {type(exc).__name__}: {exc})")
        return {}


def _reconciliation_counts(db) -> dict[str, int]:
    processing_units = db.scalar(
        select(func.count()).select_from(ExtractionUnit)
        .where(ExtractionUnit.status == "processing")
    ) or 0
    queued_documents = db.scalar(
        select(func.count()).select_from(Document)
        .where(Document.status == "queued")
    ) or 0
    resettable_units = db.scalar(
        select(func.count()).select_from(ExtractionUnit)
        .join(ExtractionSession, ExtractionSession.id == ExtractionUnit.session_id)
        .join(Document, Document.id == ExtractionSession.document_id)
        .where(
            ExtractionUnit.status == "queued",
            ExtractionSession.status == "processing",
            Document.status == "processing",
            Document.current_session_id == ExtractionSession.id,
        )
    ) or 0
    return {
        "processing_units": processing_units,
        "queued_documents": queued_documents,
        "resettable_units": resettable_units,
    }


def apply_reconciliation(depths: dict[str, int], *, workers_stopped: bool) -> int:
    """Move current queued OCR work back to Postgres, then clear only `ocr`.

    The control queue is never deleted: it may contain document setup or
    maintenance work that cannot be reconstructed from an extraction-unit row.
    """
    if not workers_stopped:
        print("REFUSED: pass --confirm-workers-stopped only after stopping Beat, "
              "worker-control and worker-ocr.")
        return 2

    with SessionLocal() as db:
        counts = _reconciliation_counts(db)
    if counts["processing_units"]:
        print(f"REFUSED: {counts['processing_units']} unit(s) are still processing. "
              "Allow graceful shutdown to finish first.")
        return 2
    if counts["queued_documents"] or depths.get("control", 0):
        print("REFUSED: document/control work is still queued. Drain or account for "
              "the control queue before reconciling OCR units.")
        return 2

    with SessionLocal.begin() as db:
        units = db.scalars(
            select(ExtractionUnit)
            .join(ExtractionSession, ExtractionSession.id == ExtractionUnit.session_id)
            .join(Document, Document.id == ExtractionSession.document_id)
            .where(
                ExtractionUnit.status == "queued",
                ExtractionSession.status == "processing",
                Document.status == "processing",
                Document.current_session_id == ExtractionSession.id,
            )
            .with_for_update(skip_locked=False)
        ).all()
        for unit in units:
            unit.status = "pending"
            unit.queued_at = None
            unit.worker_id = None
            unit.heartbeat_at = None
            unit.error_code = None
            unit.error_message = None

    import redis

    client = redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=5)
    removed = int(client.delete("ocr"))
    print(f"APPLIED: returned {len(units)} queued unit(s) to pending; "
          f"cleared current OCR queue key={bool(removed)}. Control was untouched.")
    return 0


def bound_deadlines(db) -> tuple[int, int]:
    """Units and documents that still hold their slot in the bound.

    Not the same as "status is queued or processing". A `queued` row is evidence
    that a publish was *attempted*, not that a message exists: a broker that
    refused the publish, or a worker that died after the row was flipped, both
    leave a row with no message behind it.

    So a never-started row counts against the bound -- dispatch_units gives it
    room exactly as it would live work, which is why the window reads as full
    here. What it does *not* mean is that work is happening, and that is the
    distinction the "never started" note below makes.

    The reclaim threshold is applied the same way the code applies it, so the
    two agree on what counts as stranded.
    """
    cutoff = _recent_cutoff()

    units = db.scalar(
        select(func.count()).select_from(ExtractionUnit).where(
            ExtractionUnit.status.in_(["queued", "processing"]),
            or_(
                ExtractionUnit.started_at.is_(None),
                ExtractionUnit.heartbeat_at >= cutoff,
            ),
        )
    ) or 0
    docs = db.scalar(
        select(func.count()).select_from(Document).where(
            Document.status.in_(["queued", "processing"]),
            or_(
                Document.locked_at.is_(None),
                Document.locked_at >= cutoff,
            ),
        )
    ) or 0
    return units, docs


def _recent_cutoff():
    """How far back a heartbeat still counts as a live claim.

    Twice the stale threshold: comfortably recent, but not "still in flight" by
    assumption. A row older than this is left for the reclaim path rather than
    counted against the window.
    """
    from datetime import timedelta

    from app.workflow import now

    stale = max(config.UNIT_STALE_SECONDS, config.QUEUED_RECOVERY_SECONDS)
    return now() - timedelta(seconds=stale * 2)


def report_units(db) -> tuple[dict[str, int], int]:
    counts = dict(
        db.execute(
            select(ExtractionUnit.status, func.count())
            .group_by(ExtractionUnit.status)
        ).all()
    )
    in_flight = counts.get("queued", 0) + counts.get("processing", 0)
    return counts, in_flight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="apply the safe current-queue reconciliation")
    parser.add_argument("--confirm-workers-stopped", action="store_true",
                        help="confirm Beat and both Celery workers are stopped")
    args = parser.parse_args(argv)

    print(f"Queue window: OCR_MAX_INFLIGHT_UNITS={config.OCR_MAX_INFLIGHT_UNITS}, "
          f"DOCUMENT_MAX_INFLIGHT={config.DOCUMENT_MAX_INFLIGHT}, "
          f"QUEUED_RECOVERY_SECONDS={config.QUEUED_RECOVERY_SECONDS}")

    depths = broker_depths()
    if depths:
        print("\nBroker")
        for name, depth in depths.items():
            print(f"  {name:<10} {depth:>6} message(s) waiting")
        limit = config.OCR_MAX_INFLIGHT_UNITS if "ocr" in depths else None
        if limit and depths.get("ocr", 0) > limit:
            print(f"  WARNING: the ocr queue holds {depths['ocr']} messages but the window is "
                  f"{limit}. This is the pre-fix state -- units were published without a "
                  f"bound and are queued behind work already there.")

    with SessionLocal() as db:
        unit_counts, in_flight = report_units(db)
        total_units = sum(unit_counts.values())
        live_units, live_docs = bound_deadlines(db)
        reconciliation = _reconciliation_counts(db)

        print("\nDatabase: extraction_units")
        for status in ("pending", "queued", "processing", "retry", "failed", "completed"):
            if status in unit_counts:
                print(f"  {status:<12} {unit_counts[status]:>6}")
        print(f"  {'TOTAL':<12} {total_units:>6}")

        doc_counts = dict(
            db.execute(select(Document.status, func.count()).group_by(Document.status)).all()
        )
        print("\nDatabase: documents")
        for status in ("pending", "queued", "processing", "retry", "failed", "completed"):
            if status in doc_counts:
                print(f"  {status:<12} {doc_counts[status]:>6}")

        # The one comparison that says whether the pipeline is healthy: rows
        # against the window they must fit in. Status counts above cannot show
        # this, because "queued" claims a message exists and that is exactly the
        # thing that is allowed to be untrue.
        print("\nAgainst the bounds")
        print(f"  units holding a live claim   {live_units:>6}   window {config.OCR_MAX_INFLIGHT_UNITS}")
        print(f"  documents in flight          {live_docs:>6}   bound  {config.DOCUMENT_MAX_INFLIGHT}")

        if depths:
            print(f"  ocr messages waiting         {depths.get('ocr', 0):>6}   window {config.OCR_MAX_INFLIGHT_UNITS}")
            if live_units > config.OCR_MAX_INFLIGHT_UNITS:
                print(f"\n  WARNING: {live_units} units hold a live claim but the window is "
                      f"{config.OCR_MAX_INFLIGHT_UNITS}. Either workers are behind, or rows "
                      f"were queued by a build without the window. The excess is reclaimed by "
                      f"dispatch_units after {config.QUEUED_RECOVERY_SECONDS}s.")
            elif live_units > depths.get("ocr", 0) + depths.get("control", 0):
                print(f"\n  WARNING: {live_units} units hold a live claim but the broker is holding "
                      f"{depths.get('ocr', 0) + depths.get('control', 0)} message(s). Rows the "
                      f"broker is not offering are stranded and will be reclaimed after "
                      f"{config.QUEUED_RECOVERY_SECONDS}s.")

        if live_docs > config.DOCUMENT_MAX_INFLIGHT:
            print(f"\n  WARNING: {live_docs} documents are in flight, above the bound of "
                  f"{config.DOCUMENT_MAX_INFLIGHT}.")

        never_started = db.scalar(
            select(func.count()).select_from(ExtractionUnit)
            .where(ExtractionUnit.status == "queued", ExtractionUnit.started_at.is_(None))
        ) or 0
        if never_started:
            print(f"\n  NOTE: {never_started} queued unit(s) have never started. They count "
                  f"against the window above because dispatch_units cannot tell them from live "
                  f"work, but no message is behind them -- they were queued by a build without "
                  f"the window. They reclaim to pending after {config.QUEUED_RECOVERY_SECONDS}s. "
                  f"The window will read full until then, which is the one thing to remember "
                  f"when reading these numbers.")

        stale = db.scalar(
            select(func.count()).select_from(ExtractionUnit)
            .where(ExtractionUnit.status == "processing", ExtractionUnit.heartbeat_at.is_(None))
        )
        if stale:
            print(f"\n  {stale} processing unit(s) have no heartbeat. These are claimed rows that "
                  f"never stamped a timestamp; recover() leaves them alone on purpose, because "
                  f"it cannot tell whether a worker is attached.")

    print("\nReconciliation")
    print(f"  resettable queued units      {reconciliation['resettable_units']:>6}")
    print(f"  processing-unit blockers     {reconciliation['processing_units']:>6}")
    print(f"  queued-document blockers     {reconciliation['queued_documents']:>6}")
    if not args.apply:
        print("  DRY RUN: no rows or broker messages changed")
        return 0

    return apply_reconciliation(depths, workers_stopped=args.confirm_workers_stopped)


if __name__ == "__main__":
    raise SystemExit(main())
