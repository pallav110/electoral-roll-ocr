"""Shared dashboard queries for live extraction progress."""
from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.document_session import active_session, ui_workflow_status
from app.models import ExtractionSession, ExtractionUnit, PDFDocument, ProcessingStatus, RawResponse
from app.pipeline_logging import extraction_method_label, record_preview
from app.workflow_progress import document_rank_and_total, session_page_totals, session_record_totals

_DATA_PREVIEW_LIMIT = max(0, int(os.getenv("PIPELINE_DATA_PREVIEW_LIMIT", "25")))


def processing_count(counts: dict) -> int:
    return (
        counts.get("pending", 0)
        + counts.get("queued", 0)
        + counts.get("processing", 0)
        + counts.get("retry", 0)
    )


def ui_status_counts(db: Session) -> dict[str, int]:
    ui_counts = {k: 0 for k in ("pending", "queued", "processing", "retry", "failed", "completed", "cancelled")}
    docs = db.scalars(select(PDFDocument)).all()
    for doc in docs:
        ui = ui_workflow_status(doc, active_session(db, doc))
        if ui in ui_counts:
            ui_counts[ui] += 1
        elif doc.status == ProcessingStatus.SKIPPED_DUPLICATE.value:
            ui_counts["cancelled"] += 1
    return ui_counts


def _active_unit(db: Session, session_id) -> ExtractionUnit | None:
    return db.scalar(
        select(ExtractionUnit)
        .where(
            ExtractionUnit.session_id == session_id,
            ExtractionUnit.status == "processing",
        )
        .order_by(ExtractionUnit.unit_number)
        .limit(1)
    )


def _unit_has_raw(db: Session, unit_id) -> bool:
    return (
        db.scalar(
            select(RawResponse.id)
            .where(RawResponse.extraction_unit_id == unit_id)
            .limit(1)
        )
        is not None
    )


def unit_pipeline_stage(db: Session, unit: ExtractionUnit) -> tuple[str, str]:
    """Return (stage_key, activity) aligned with worker/OCR log prefixes."""
    n, pf, pt = unit.unit_number, unit.page_from, unit.page_to
    if unit.status == "completed":
        rec = unit.records_extracted or 0
        return "done", f"[DB] Unit {n} completed — {rec} record(s) saved (pages {pf}–{pt})"
    if unit.status in ("pending", "queued", "retry"):
        return "queued", f"[WORKER] Queued — unit {n}, pages {pf}–{pt}"
    if unit.status == "processing":
        if _unit_has_raw(db, unit.id):
            return "db", f"[DB] Writing to PostgreSQL — unit {n}, pages {pf}–{pt} (normalize + insert)"
        return "ocr", f"[EXTRACT] Waiting for OCR service — unit {n}, pages {pf}–{pt}"
    if unit.status == "failed":
        msg = (unit.error_message or unit.error_code or "failed")[:120]
        return "failed", f"[FAILED] Unit {n} — {msg}"
    return "other", f"Unit {n} — {unit.status}"


def _record_previews_from_raw(raw_payload: dict | None, *, limit: int) -> list[str]:
    if not raw_payload or not isinstance(raw_payload, dict):
        return []
    records = raw_payload.get("records")
    if not isinstance(records, list):
        return []
    previews: list[str] = []
    for idx, item in enumerate(records[:limit], 1):
        if isinstance(item, dict):
            previews.append(record_preview(idx, item))
    if limit and len(records) > limit:
        previews.append(f"… {len(records) - limit} more record(s) (preview limit {limit})")
    return previews


def _live_record_previews(db: Session, session: ExtractionSession | None, active: ExtractionUnit | None) -> list[str]:
    if not session:
        return []
    unit = active
    if unit is None:
        unit = db.scalar(
            select(ExtractionUnit)
            .where(ExtractionUnit.session_id == session.id, ExtractionUnit.status == "completed")
            .order_by(ExtractionUnit.completed_at.desc().nullslast(), ExtractionUnit.unit_number.desc())
            .limit(1)
        )
    if unit is None:
        return []
    raw = db.scalar(
        select(RawResponse)
        .where(RawResponse.extraction_unit_id == unit.id)
        .order_by(RawResponse.id.desc())
        .limit(1)
    )
    if not raw:
        return []
    payload = raw.raw_response if isinstance(raw.raw_response, dict) else None
    limit = min(8, _DATA_PREVIEW_LIMIT) if _DATA_PREVIEW_LIMIT else 8
    return _record_previews_from_raw(payload, limit=limit)


def document_progress_detail(db: Session, doc: PDFDocument, session: ExtractionSession | None) -> dict:
    """Real DB-backed status for UI (aligned with pipeline log lines)."""
    pages_done = pages_total = records_extracted = records_saved = 0
    units_done = units_total = units_active = None
    worker_id = None
    activity = None
    stage = None
    error = None
    active_unit = None
    pdf_rank = pdf_total = 0
    ui_status = ui_workflow_status(doc, session)
    extraction_method = extraction_method_label()

    if ui_status == "failed":
        error = doc.error_message or doc.error_type
    elif ui_status == "retry":
        activity = "[WORKER] Waiting for retry"
        error = doc.error_message

    if session:
        pdf_rank, pdf_total = document_rank_and_total(db, doc.id)
        pages_done, pages_total = session_page_totals(db, session.id)
        if pages_total == 0 and session.pages_total:
            pages_total = session.pages_total
        records_extracted, records_saved = session_record_totals(db, session.id)
        if session.pages_processed and session.pages_processed > pages_done:
            pages_done = session.pages_processed
        if session.records_extracted and session.records_extracted > records_extracted:
            records_extracted = session.records_extracted
        stats = db.execute(
            select(
                func.count(ExtractionUnit.id),
                func.count().filter(ExtractionUnit.status == "completed"),
                func.count().filter(ExtractionUnit.status.in_(("processing", "queued", "pending", "retry"))),
            ).where(ExtractionUnit.session_id == session.id)
        ).one()
        units_total, units_done, units_active = stats[0], stats[1], stats[2]
        active_unit = _active_unit(db, session.id)
        if active_unit:
            worker_id = active_unit.worker_id
            stage, activity = unit_pipeline_stage(db, active_unit)
        elif ui_status == "queued":
            stage, activity = "queued", "[WORKER] Queued — waiting for worker"
        elif ui_status == "processing" and units_active:
            stage, activity = "queued", f"[WORKER] {units_active} unit(s) queued — waiting for worker slot"
        elif ui_status == "processing" and pages_total and pages_done < pages_total:
            stage, activity = "ocr", f"[PAGE] Next page {pages_done + 1}/{pages_total}"
        elif session.status == "processing":
            stage, activity = "ocr", "[EXTRACT] Processing"
        if session.error_message and ui_status == "failed":
            error = session.error_message

    if ui_status == "pending":
        stage, activity = "pending", "[SCAN] Pending — not yet dispatched"

    progress_line = None
    if session and (pages_total or records_extracted or records_saved):
        pdf_part = f"PDFs: {pdf_rank}/{pdf_total}" if pdf_total else "PDFs: —"
        progress_line = (
            f"[PROGRESS] {pdf_part} | Pages: {pages_done}/{pages_total or '—'} | "
            f"Extracted: {records_extracted} | Saved: {records_saved}"
        )

    record_previews = _live_record_previews(db, session, active_unit) if session else []

    return {
        "ui_status": ui_status,
        "pages_done": pages_done,
        "pages_total": pages_total or (session.pages_total if session else 0),
        "records_extracted": records_extracted,
        "records_saved": records_saved,
        "units_done": units_done,
        "units_total": units_total,
        "units_active": units_active,
        "worker_id": worker_id or (session.worker_id if session else None),
        "activity": activity,
        "stage": stage,
        "progress_line": progress_line,
        "extraction_method": extraction_method,
        "active_unit_number": active_unit.unit_number if active_unit else None,
        "active_page_from": active_unit.page_from if active_unit else None,
        "active_page_to": active_unit.page_to if active_unit else None,
        "record_previews": record_previews,
        "error": error,
    }


def live_extraction_rows(db: Session) -> list[dict]:
    docs = db.scalars(
        select(PDFDocument)
        .where(PDFDocument.status.in_((ProcessingStatus.PENDING.value, ProcessingStatus.PROCESSING.value)))
        .order_by(PDFDocument.processing_started_at.desc().nullslast(), PDFDocument.discovered_at.desc())
        .limit(50)
    ).all()
    rows = []
    for doc in docs:
        session = active_session(db, doc)
        detail = document_progress_detail(db, doc, session)
        if detail["ui_status"] not in ("pending", "queued", "processing", "retry"):
            continue
        rows.append({"doc": doc, "session": session, **detail})
    return rows


def recent_failed_rows(db: Session, limit: int = 8) -> list[dict]:
    docs = db.scalars(
        select(PDFDocument)
        .where(PDFDocument.status == ProcessingStatus.FAILED.value)
        .order_by(PDFDocument.processing_completed_at.desc().nullslast())
        .limit(limit)
    ).all()
    return [{"doc": d, **document_progress_detail(db, d, active_session(db, d))} for d in docs]
