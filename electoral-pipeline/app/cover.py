"""Read each roll's cover page once and keep what it says.

The cover (page 1) names the state, constituency, part, polling station and
-- in its section 4 -- the printed voter counts: male, female, third gender,
total. Those counts are the yardstick the card OCR is measured against, so
they are read by their own small job rather than as a side effect of the
multi-hour card extraction:

- `sweep_missing_covers()` runs from the beat. It finds documents with no
  cover row, inserts a `queued` row for each (bounded by COVER_SWEEP_BATCH)
  and enqueues `read_document_cover` on the `cover` queue.
- `read_cover(document_id)` is the task body. It posts the PDF to the OCR
  service's /ocr/cover endpoint and stores the answer on that row.

A failed read is retried on the next sweep until COVER_MAX_ATTEMPTS; after
that the row stays `failed` with the error so it is visible, not silent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import SessionLocal
from app.models import Document, DocumentCover

log = logging.getLogger(__name__)

_FIELD_KEYS = (
    "roll_year", "state_code", "state_name", "ac_code", "ac_name", "ac_reservation",
    "part_number", "pc_code", "pc_name", "pc_reservation", "revision_year",
    "qualifying_date", "revision_type", "publication_date", "roll_identification",
    "sections", "village", "ward", "post_office", "police_station", "panchayat",
    "block", "tehsil", "district", "pin_code", "polling_station_number",
    "polling_station_name", "polling_station_address", "polling_station_type",
    "auxiliary_stations", "total_pages",
)
_COUNT_KEYS = ("start_serial", "end_serial", "male", "female", "third_gender", "total")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def documents_without_cover(db: Session, limit: int) -> list[Document]:
    """Documents that have no cover row, or a failed one still under the retry cap."""
    missing = (
        select(Document)
        .outerjoin(DocumentCover, DocumentCover.document_id == Document.id)
        .where(
            Document.status != "cancelled",
            (DocumentCover.id.is_(None))
            | (
                (DocumentCover.status == "failed")
                & (DocumentCover.attempt_count < config.COVER_MAX_ATTEMPTS)
            ),
        )
        .order_by(Document.discovered_at.asc())
        .limit(limit)
    )
    return list(db.scalars(missing).all())


def sweep_missing_covers(enqueue) -> int:
    """Queue a cover read for every document that lacks one. Returns how many."""
    queued = 0
    with SessionLocal() as db:
        for document in documents_without_cover(db, config.COVER_SWEEP_BATCH):
            row = db.scalar(select(DocumentCover).where(DocumentCover.document_id == document.id))
            if row is None:
                row = DocumentCover(document_id=document.id)
                db.add(row)
            row.status = "queued"
            row.queued_at = _now()
            db.commit()
            enqueue(str(document.id))
            queued += 1
    if queued:
        log.info("[COVER] queued %d cover read(s)", queued)
    return queued


def _apply_payload(row: DocumentCover, payload: dict) -> None:
    fields = payload.get("fields") or {}
    counts = payload.get("counts") or {}
    for key in _FIELD_KEYS:
        value = fields.get(key)
        if isinstance(value, str):
            value = value.strip() or None
        setattr(row, key, value)
    for key in _COUNT_KEYS:
        setattr(row, key, counts.get(key))
    row.counts_consistent = payload.get("counts_consistent")
    row.source = payload.get("source")
    row.raw_text = payload.get("raw_text")
    row.payload = {k: v for k, v in payload.items() if k != "raw_text"}


def read_cover(document_id: str) -> dict:
    """Task body: fetch page-1 details for one document and store them."""
    with SessionLocal() as db:
        document = db.get(Document, document_id)
        if document is None:
            return {"document_id": document_id, "status": "missing"}
        row = db.scalar(select(DocumentCover).where(DocumentCover.document_id == document.id))
        if row is None:
            row = DocumentCover(document_id=document.id)
            db.add(row)
        row.status = "processing"
        row.attempt_count = (row.attempt_count or 0) + 1
        db.commit()

        try:
            with open(document.source_path, "rb") as handle:
                response = httpx.post(
                    config.COVER_EXTRACTOR_URL,
                    files={"pdf_file": (document.file_name, handle, "application/pdf")},
                    headers={"X-API-Key": config.EXTRACTOR_API_KEY} if config.EXTRACTOR_API_KEY else {},
                    timeout=config.COVER_TIMEOUT_SECONDS,
                )
            if response.status_code >= 400:
                detail = ""
                try:
                    detail = str(response.json().get("detail", ""))
                except Exception:  # noqa: BLE001 -- body is diagnostic only
                    detail = response.text[:300]
                raise RuntimeError(f"cover reader returned {response.status_code}: {detail}")
            payload = response.json()
            _apply_payload(row, payload)
            row.status = "completed"
            row.error = None
            row.extracted_at = _now()
            db.commit()
            log.info(
                "[COVER] %s: total=%s male=%s female=%s third=%s (%s)",
                document.file_name, row.total, row.male, row.female, row.third_gender, row.source,
            )
            return {"document_id": document_id, "status": "completed", "total": row.total}
        except Exception as exc:  # noqa: BLE001 -- recorded on the row, retried by the sweep
            db.rollback()
            row = db.scalar(select(DocumentCover).where(DocumentCover.document_id == document.id))
            if row is not None:
                row.status = "failed"
                row.error = f"{type(exc).__name__}: {exc}"[:2000]
                # The reader was unreachable (restarting, or not up yet), so
                # the cover was never actually looked at. Counting that as an
                # attempt let three service restarts permanently fail a cover
                # that nobody had tried to read. Give the attempt back; the
                # next sweep retries it.
                if isinstance(exc, httpx.TransportError):
                    row.attempt_count = max(0, (row.attempt_count or 1) - 1)
                db.commit()
            log.warning("[COVER] %s failed: %s", document.file_name, exc)
            return {"document_id": document_id, "status": "failed", "error": str(exc)}
