#!/usr/bin/env python3
"""Render the FLAGGED cards for human reading -- the manual check, on demand.

Run this after an extraction. It re-runs the same review triggers the service
applies and renders only the records that come out flagged, so a human reads 11
cards instead of 531, and the 11 are the ones the pipeline itself said it was
unsure about. The triggers are IMPORTED from ocr_pdf_api, never reimplemented
here, so the picture and the flag can never disagree about what is uncertain.

Method, carried over from the 13-card verification and for the same reasons:

  * rendered from the PDF's VECTOR page at 400 DPI, not from a 150 DPI JPEG
    strip, so there is no generation loss between the card and the reader
  * NO pipeline value is drawn on the image. Overlaying "we read X" invites the
    reader to confirm X, which is the single biggest way a manual check stops
    being independent evidence. The card alone goes in the picture; our value
    lives in the manifest BESIDE it, on a line below a blank to fill in.
  * the whole card, not a guessed row crop

Usage:
    python tests/test_scripts/render_flagged_cards.py --results capture.json \\
        --output-dir /tmp/review [--max-cards 40]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import cv2
import fitz
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = ROOT / "input" / "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
DEFAULT_RESULTS = ROOT / "results" / "ocr_debug_output" / "before.raw.json"
DEFAULT_OUTPUT_DIR = ROOT / "results" / "flagged_cards"
DPI = 400

# Which field to show for each review reason. A reason names a class of problem
# and the reader needs the field it is about -- otherwise the card is just a
# picture and the reader has to guess where to look.
REASON_FIELD = {
    "age_absent": "age",
    "age_not_integer": "age",
    "age_out_of_range": "age",
    "house_absent": "house_no",
    "house_devanagari_unknown": "house_no",
    "house_leading_1_prepended": "house_no",
    "epic_readers_disagree": "id_card_no",
}


def _load_ocr_api():
    sys.path.insert(0, str(ROOT))
    import ocr_pdf_api

    return ocr_pdf_api


def load_records(path: Path) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload["records"] if isinstance(payload, dict) else payload


def flagged_records(api, records: list[dict]) -> list[tuple[dict, list[str]]]:
    """Re-apply the service's own triggers. Pure: it reads and adds flags."""
    flagged = []
    for src in records:
        rec = dict(src)
        api._review_record(rec)
        if rec.get("_needs_review"):
            flagged.append((src, rec.get("_review_reasons", [])))
    flagged.sort(key=lambda pair: (pair[0].get("page_number") or 0, pair[0].get("card_index") or 0))
    return flagged


def write_manifest(path: Path, flagged: list[tuple[dict, list[str]]], limit: int) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write("Cards the pipeline flagged as uncertain.\n\n")
        fh.write("For each: open the PNG, read the field, write what you see on\n")
        fh.write("the LINE below it. Do not look at our value before reading the\n")
        fh.write("card -- our value is printed after yours for comparison, and\n")
        fh.write("reading it first turns an independent check into a confirmation.\n\n")
        fh.write("=" * 78 + "\n\n")
        for index, (src, reasons) in enumerate(flagged[:limit], 1):
            fh.write("%d) page %s card %s   EPIC %s\n" % (
                index, src.get("page_number"), src.get("card_index"), src.get("id_card_no", "?")))
            for reason in reasons:
                fh.write("     why : %s\n" % reason)
            fields = []
            for reason in reasons:
                field = REASON_FIELD.get(reason.split(":")[0])
                if field and field not in fields:
                    fields.append(field)
            for field in fields:
                fh.write("     ours: %s = %r\n" % (field, src.get(field, "")))
                fh.write("     CARD: ______________________________\n")
            fh.write("\n")


def render_cards(api, pdf_path: Path, flagged, out_dir: Path, limit: int) -> int:
    doc = fitz.open(pdf_path)
    written = 0
    try:
        for src, _ in flagged[:limit]:
            page_no = src.get("page_number")
            card_no = src.get("card_index")
            if not page_no or not card_no:
                continue
            page_obj = doc[page_no - 1]
            rects = api._voter_card_rects(page_obj)
            if not rects or not (1 <= card_no <= len(rects)):
                continue
            zoom = DPI / 72.0
            pix = page_obj.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rects[card_no - 1])
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            cv2.imwrite(str(out_dir / ("p%02d_c%02d.png" % (page_no, card_no))), img)
            written += 1
    finally:
        doc.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-cards", type=int, default=40)
    args = parser.parse_args()

    api = _load_ocr_api()
    records = load_records(args.results)
    flagged = flagged_records(api, records)

    out_dir = Path(args.output_dir)
    os.makedirs(out_dir, exist_ok=True)
    write_manifest(out_dir / "manifest.txt", flagged, args.max_cards)
    written = render_cards(api, args.pdf, flagged, out_dir, args.max_cards)

    print("flagged %d of %d records (%.1f%%)" % (
        len(flagged), len(records), 100.0 * len(flagged) / max(1, len(records))))
    for reason, count in Counter(r for _, reasons in flagged for r in reasons).most_common():
        print("   %-34s %d" % (reason.split(":")[0], count))
    print("wrote %d cards + manifest.txt to %s" % (written, out_dir))


if __name__ == "__main__":
    main()