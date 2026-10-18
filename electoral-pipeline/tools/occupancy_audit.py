"""Corpus-wide shadow occupancy audit without exporting voter fields.

Reads only document source paths and record page/card positions from the local
Postgres database. It writes aggregate audit metrics and image crops only for
predicted-blank slots that have an existing saved record (the safety-critical
false-blank cases). It never writes names, EPICs, or record payloads.

Run inside the OCR container:
    python occupancy_audit.py --out /tmp/occupancy-audit
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/app")

import cv2
import fitz
import numpy as np
from sqlalchemy import select

import ocr_pdf_api as A
from app.db import SessionLocal
from app.models import Document, ElectoralRecord

DPI = 100
SCALE = DPI / 72.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/occupancy-audit")
    args = parser.parse_args()
    out = Path(args.out)
    cases = out / "false_blank_cases"
    cases.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        docs = db.execute(
            select(Document.id, Document.file_name, Document.source_path)
            .where(Document.status == "completed")
            .order_by(Document.file_name)
        ).all()
        record_rows = db.execute(
            select(ElectoralRecord.document_id, ElectoralRecord.page_number,
                   ElectoralRecord.card_index)
        ).all()

    expected = {}
    for document_id, page_number, card_index in record_rows:
        if page_number is None or card_index is None:
            continue
        expected.setdefault(str(document_id), set()).add(
            (int(page_number), int(card_index))
        )

    totals = Counter()
    disagreements = []
    started = time.perf_counter()
    processed_docs = 0
    processed_pages = 0

    for document_id, file_name, source_path in docs:
        path = Path(source_path)
        if not path.exists():
            totals["missing_source"] += 1
            continue
        try:
            pdf = fitz.open(path)
        except Exception:
            totals["unreadable_pdf"] += 1
            continue
        doc_key = str(document_id)
        saved = expected.get(doc_key, set())
        try:
            for page_index in range(len(pdf)):
                page_number = page_index + 1
                page = pdf[page_index]
                pix = page.get_pixmap(dpi=DPI, alpha=False)
                raw = np.frombuffer(pix.samples, np.uint8).reshape(
                    pix.height, pix.width, pix.n)[:, :, :3]
                rects = A._voter_card_rects(page)
                for card_index, rect in enumerate(rects):
                    crop = raw[int(rect.y0 * SCALE):int(rect.y1 * SCALE),
                               int(rect.x0 * SCALE):int(rect.x1 * SCALE)].copy()
                    state = A._classify_card_occupancy(crop)
                    has_record = (page_number, card_index) in saved
                    totals[f"state_{state}"] += 1
                    totals["records"] += int(has_record)
                    if state == A.CARD_OCCUPANCY_BLANK and has_record:
                        totals["false_blank"] += 1
                        if len(disagreements) < 200:
                            safe = f"{len(disagreements):04d}_p{page_number}_c{card_index}.png"
                            cv2.imwrite(str(cases / safe),
                                         cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
                            disagreements.append({
                                "file": file_name,
                                "page": page_number,
                                "card": card_index,
                                "crop": str(cases / safe),
                            })
                    elif state == A.CARD_OCCUPANCY_OCCUPIED and not has_record:
                        totals["occupied_without_record"] += 1
                    elif state == A.CARD_OCCUPANCY_UNCERTAIN:
                        totals["uncertain_with_record"] += int(has_record)
                processed_pages += 1
            processed_docs += 1
        finally:
            pdf.close()
        if processed_docs % 5 == 0:
            print(f"processed_docs={processed_docs} pages={processed_pages}", flush=True)

    result = {
        "documents_seen": len(docs),
        "documents_processed": processed_docs,
        "pages_processed": processed_pages,
        "seconds": round(time.perf_counter() - started, 1),
        "counts": dict(totals),
        "false_blank_cases": disagreements,
        "authority_safe": totals["false_blank"] == 0,
    }
    (out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
