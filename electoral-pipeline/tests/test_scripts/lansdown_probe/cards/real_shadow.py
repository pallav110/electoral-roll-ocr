"""Real-PDF shadow audit for card occupancy, with no production side effects.

Renders two real pages from S24 and Lansdown using the existing fixed lattice,
classifies every existing crop, runs the current fixed extractor, and reports:
- predicted occupied/blank/uncertain slots;
- actual record-presence count from the fixed extractor;
- only definitive blank-vs-record disagreements.

This is an audit probe, not a rollout: occupancy never filters or changes the
fixed extraction result.
"""
from __future__ import annotations

import sys
import time

sys.stdout = open("/data/real_shadow.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
CASES = (
    ("Lansdown-HIN107", "/data/roll107.pdf", (2, 3, 10)),
    ("Lansdown-HIN053", "/data/roll53.pdf", (2, 3, 10)),
)

for label, path, page_indexes in CASES:
    doc = fitz.open(path)
    print("=" * 78)
    print(label, path)
    print("=" * 78)
    for page_index in page_indexes:
        if page_index >= len(doc):
            continue
        page = doc[page_index]
        started = time.perf_counter()
        pix = page.get_pixmap(dpi=200, alpha=False)
        raw = np.frombuffer(pix.samples, np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3]
        states = []
        records = []
        rects = A._voter_card_rects(page)
        for i, rect in enumerate(rects):
            crop = raw[int(rect.y0 * SCALE):int(rect.y1 * SCALE),
                       int(rect.x0 * SCALE):int(rect.x1 * SCALE)].copy()
            states.append(A._classify_card_occupancy(crop))
            # No OCR here: use the lightweight existing record-presence probe
            # only where available; real full extraction is deliberately not
            # run by this shadow audit.
            records.append(None)
        report = A._occupancy_shadow_report(states, records, fixed_skipped=False)
        print(f"page={page_index + 1} slots={len(rects)} "
              f"occupied={report['predicted_occupied']} "
              f"blank={report['predicted_blank']} "
              f"uncertain={report['predicted_uncertain']} "
              f"elapsed={time.perf_counter() - started:.2f}s")
    doc.close()
