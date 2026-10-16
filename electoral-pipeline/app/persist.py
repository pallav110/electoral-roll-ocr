"""Persist normalized voter rows to PostgreSQL (shared by worker and OCR service)."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ExtractedRecord, ExtractedVoter, PDFDocument
from app.normalize import map_extracted_record, map_voter
from app.ocr_adapter import _metadata_from_roll, electoral_item_from_public_record
from app.pipeline_logging import pipeline_event


def voter_already_saved(db: Session, pdf_name: str, voter: ExtractedVoter) -> bool:
    epic = (voter.id_card_no or "").strip()
    if epic:
        found = db.scalar(
            select(ExtractedVoter.sno)
            .where(ExtractedVoter.pdf_name == pdf_name, ExtractedVoter.id_card_no == epic)
            .limit(1)
        )
        return found is not None
    sr = (voter.voter_sr_no or "").strip()
    if sr:
        found = db.scalar(
            select(ExtractedVoter.sno)
            .where(ExtractedVoter.pdf_name == pdf_name, ExtractedVoter.voter_sr_no == sr)
            .limit(1)
        )
        return found is not None
    return False


def insert_voters(
    db: Session,
    *,
    contract_records: list[dict],
    pdf_name: str,
    roll_meta: dict | None,
    doc_row: PDFDocument | None,
    header: ExtractedRecord | None = None,
    upsert_header: bool = False,
    log_events: bool = True,
) -> tuple[int, int]:
    """Insert electoral_v1 contract records. Returns (inserted, skipped_as_duplicate)."""
    roll_meta = roll_meta or {}
    inserted = skipped = 0
    attempted = sum(1 for item in contract_records if not (item.get("common") or {}).get("is_deleted"))
    if log_events and attempted:
        pipeline_event(
            "DB",
            "Saving %s extracted record(s) to PostgreSQL (pdf_processor_extractedvoter)",
            attempted,
        )
    if upsert_header and header is not None:
        values = {
            c.name: getattr(header, c.key)
            for c in ExtractedRecord.__table__.columns
            if c.name not in ("id", "extracted_at")
        }
        db.execute(
            insert(ExtractedRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ExtractedRecord.data_hash])
        )
        if doc_row and header.booth_no:
            doc_row.booth_no = header.booth_no
    for item in contract_records:
        if (item.get("common") or {}).get("is_deleted"):
            continue
        voter = map_voter(item, pdf_name=pdf_name, roll=roll_meta)
        if voter_already_saved(db, pdf_name, voter):
            skipped += 1
            continue
        voter.sno = db.scalar(text("SELECT nextval('pdf_processor_extractedvoter_sno_seq')"))
        if doc_row and doc_row.config_id and header:
            voter.state_code = voter.state_code or header.state_code
        values = {c.name: getattr(voter, c.key) for c in ExtractedVoter.__table__.columns}
        db.execute(insert(ExtractedVoter).values(**values))
        inserted += 1
    if log_events and attempted:
        pipeline_event(
            "DB",
            "Successfully saved %s record(s) to PostgreSQL (%s skipped duplicate)",
            inserted,
            skipped,
        )
    return inserted, skipped


def persist_public_records_batch(
    public_records: list[dict],
    *,
    pdf_document_id: int,
    pdf_name: str,
    roll_metadata: dict | None,
    page_from: int,
    page_to: int,
    upsert_header: bool = False,
) -> tuple[int, int]:
    """Map OCR public records and insert voters (one DB transaction per flush batch)."""
    if not public_records:
        return 0, 0
    from app.db import SessionLocal

    items = [
        electoral_item_from_public_record(row, page_from, page_to)
        for row in public_records
        if not row.get("is_deleted")
    ]
    if not items:
        return 0, 0
    roll_metadata = roll_metadata or {}
    roll_common = (_metadata_from_roll(roll_metadata).get("common") or {})
    with SessionLocal.begin() as db:
        doc_row = db.get(PDFDocument, pdf_document_id)
        header = None
        if upsert_header:
            header = map_extracted_record(
                {"document_metadata": _metadata_from_roll(roll_metadata)},
                pdf_document_id,
                doc_row.config_id if doc_row else None,
                pdf_name,
            )
        inserted, skipped = insert_voters(
            db,
            contract_records=items,
            pdf_name=pdf_name,
            roll_meta=roll_common,
            doc_row=doc_row,
            header=header,
            upsert_header=upsert_header,
            log_events=False,
        )
    return inserted, skipped
