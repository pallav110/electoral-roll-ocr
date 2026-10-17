"""Measure per-page wall-clock, to check it against the 7-9s/page target.

The service already prints `page=<n> complete ... elapsed=<t>s`.  This runs the
real extraction path over a handful of pages of each roll and reports the
distribution, plus the phase breakdown that explains it, so a regression can be
attributed to a phase rather than guessed at.

Run inside the OCR container, where the module is the live bind-mounted copy.
"""
import sys
import time

sys.stdout = open("/data/timing.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

CASES = (
    ("S28-40 Lansdown", "/data/roll107.pdf", [2, 3, 4]),
    ("S24-53", "/data/roll_s24_300.pdf", [2, 3, 4]),
)

SCALE = 200 / 72.0

print("=" * 78)
print(f"workers={A.OCR_CARD_WORKERS if hasattr(A, 'OCR_CARD_WORKERS') else '?'}"
      f"   tesseract_min_height={A.TESSERACT_MIN_HEIGHT_PX}")
print("=" * 78)

for label, path, pages in CASES:
    doc = fitz.open(path)
    print(f"\n### {label}   {doc.page_count} pages, sampling {pages}")
    for index in pages:
        page = doc[index]
        t0 = time.perf_counter()
        pix = page.get_pixmap(dpi=200, alpha=False)
        raw = np.frombuffer(pix.samples, np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3]
        render_s = time.perf_counter() - t0

        rects = A._voter_card_rects(page)
        cards = [A._crop_card_region(raw, (
            r.x0, r.y0, r.x1, r.y1,
        ), ) if False else raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
                               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
                for r in rects]

        t1 = time.perf_counter()
        lines_and_conf = [
            A._extract_text_with_tesseract(
                cv2.imencode(".png", cv2.cvtColor(c, cv2.COLOR_RGB2BGR))[1].tobytes())
            for c in cards
        ]
        tesseract_s = time.perf_counter() - t1

        parsed = [A.parse_voter_box_from_ocr_lines(l or []) for l, _ in lines_and_conf]
        named = sum(1 for p in parsed if p.get("voter_fname") or p.get("voter_name"))

        print(f"  page {index + 1}: render {render_s:5.2f}s  "
              f"tesseract({len(cards)} cards) {tesseract_s:6.2f}s  "
              f"names {named}/{len(cards)}  "
              f"-> {render_s + tesseract_s:5.2f}s for the tesseract-only path")

        # What the sequential paddle pass adds.  One representative card is
        # enough to show the per-card cost; the whole page is extrapolated.
        if cards:
            t2 = time.perf_counter()
            try:
                A._detect_deleted_watermark(cards[0])
                paddle_s = time.perf_counter() - t2
                est = paddle_s * len(cards)
                print(f"           paddle (1 card) {paddle_s:5.2f}s  "
                      f"-> {est:6.1f}s if sequential over all {len(cards)}")
            except Exception as exc:
                print(f"           paddle probe failed: {exc}")
    doc.close()
    print()

print("=" * 78)
print("Target is 7-9s per page END TO END, including the header pass.")
print("=" * 78)