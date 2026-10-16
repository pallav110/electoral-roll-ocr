"""Read-only DB helpers for pipeline progress logging."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models import ExtractedVoter, ExtractionSession, ExtractionUnit, PDFDocument, ProcessingStatus


def document_rank_and_total(db, document_id) -> tuple[int, int]:
    doc = db.get(PDFDocument, int(document_id))
    if not doc:
        return 0, 0
    total = db.scalar(select(func.count()).select_from(PDFDocument)) or 0
    rank = db.scalar(select(func.count()).select_from(PDFDocument).where(PDFDocument.discovered_at <= doc.discovered_at)) or 0
    return int(rank), int(total)


def count_pdfs_completed(db) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PDFDocument).where(PDFDocument.status == ProcessingStatus.COMPLETED.value)
        )
        or 0
    )


def session_page_totals(db, session_id) -> tuple[int, int]:
    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    session = db.get(ExtractionSession, session_uuid)
    if not session:
        return 0, 0
    pages_total = session.pages_total or 0
    pages_done = int(
        db.scalar(
            select(func.coalesce(func.sum(ExtractionUnit.page_to - ExtractionUnit.page_from + 1), 0)).where(
                ExtractionUnit.session_id == session_uuid,
                ExtractionUnit.status == "completed",
            )
        )
        or 0
    )
    return pages_done, pages_total


def session_record_totals(db, session_id) -> tuple[int, int]:
    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    session = db.get(ExtractionSession, session_uuid)
    extracted = int(
        db.scalar(
            select(func.coalesce(func.sum(ExtractionUnit.records_extracted), 0)).where(
                ExtractionUnit.session_id == session_uuid,
                ExtractionUnit.status == "completed",
            )
        )
        or 0
    )
    saved = extracted
    if session:
        doc = db.get(PDFDocument, session.pdf_document_id)
        if doc:
            saved = int(
                db.scalar(select(func.count()).select_from(ExtractedVoter).where(ExtractedVoter.pdf_name == doc.file_name))
                or 0
            )
    return extracted, saved
