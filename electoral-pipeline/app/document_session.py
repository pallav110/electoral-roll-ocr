"""Resolve the active extraction session and document orchestration fields."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExtractionSession, PDFDocument, ProcessingStatus


def active_session(db: Session, doc: PDFDocument) -> ExtractionSession | None:
    return db.scalar(
        select(ExtractionSession)
        .where(ExtractionSession.pdf_document_id == doc.id)
        .order_by(ExtractionSession.attempt_number.desc())
        .limit(1)
    )


def ui_workflow_status(doc: PDFDocument, session: ExtractionSession | None) -> str:
    if doc.status == ProcessingStatus.FAILED.value:
        if session and session.doc_next_retry_at:
            return "retry"
        return "failed"
    if doc.status == ProcessingStatus.COMPLETED.value:
        return "completed"
    if doc.status == ProcessingStatus.PENDING.value:
        if session and session.status in ("created", "processing"):
            return "queued"
        if session and session.doc_next_retry_at:
            return "retry"
        return "pending"
    if session and session.status == "created":
        return "queued"
    if doc.status == ProcessingStatus.PROCESSING.value:
        return "processing"
    return doc.ui_status
