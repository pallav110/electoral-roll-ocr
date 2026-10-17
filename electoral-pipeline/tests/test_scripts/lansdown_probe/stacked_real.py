"""Time the REAL stacked pass, reproducing the pipeline's own construction.

The first profile was wrong.  It timed `np.vstack([full_card] * 3)` -- three
complete cards, roughly 2067x1761px -- whereas the pipeline stacks three SMALL
crops (serial, house, age) each scaled to ~80px tall.  Those are not the same
image by a wide margin, so the 24.74s it reported describes work the pipeline
never does.

This rebuilds the stack exactly as `_extract_card` does -- same region table,
same `80 // height` scale, same border padding, same single `ocr()` call -- and
reports the true image dimensions alongside the timing.
"""
import sys
import time

sys.stdout = open("/data/stacked.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0

for path, page_no, label in (("/data/roll107.pdf", 3, "S28-40 Lansdown"),
                             ("/data/roll_s24_300.pdf", 3, "S24-53")):
    doc = fitz.open(path)
    page = doc[page_no]
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    rects = A._voter_card_rects(page)
    cards = [
        raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
            int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
        for r in rects
    ]
    paddle = A._get_paddle()

    sizes, times = [], []
    for card in cards:
        clean = A._remove_outer_card_lines(card)
        if clean is None or getattr(clean, "size", 0) == 0:
            continue
        crops_info = []
        # The pipeline crops serial and house from the cleaned card and scales
        # each to a target height of about 80px.
        for field, region in (
            ("serial", A.TESSERACT_CARD_CONTENT_REGIONS[0]),
            ("house", A.TESSERACT_CARD_CONTENT_REGIONS[1]),
        ):
            crop = A._crop_card_region(clean, region, field.upper())
            if crop is not None and getattr(crop, "size", 0) > 0:
                s = min(6, max(2, 80 // max(1, crop.shape[0])))
                crops_info.append((field, cv2.resize(
                    crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)))
        if not crops_info:
            continue
        max_w = max(c.shape[1] for _, c in crops_info)
        padded, y = [], 0
        for field, c in crops_info:
            pad = cv2.copyMakeBorder(c, 4, 4, 4, max_w - c.shape[1] + 4,
                                     cv2.BORDER_CONSTANT, value=(255, 255, 255))
            y += pad.shape[0]
            padded.append(pad)
        stacked = np.vstack(padded) if len(padded) > 1 else padded[0]

        t = time.perf_counter()
        try:
            paddle.ocr(stacked, cls=False)
        except Exception as exc:
            print(f"  reader failed: {exc}")
            continue
        times.append(time.perf_counter() - t)
        sizes.append(stacked.shape)

    print("=" * 78)
    print(f"{label}   page {page_no + 1}: {len(times)} cards")
    print("=" * 78)
    if times:
        print(f"  stacked image sizes: {sorted({s for s in sizes})[:4]}")
        print(f"  total {sum(times):6.2f}s   mean {sum(times) / len(times):5.3f}s"
              f"   min {min(times):5.3f}   max {max(times):5.3f}")
        print(f"  -> {sum(times):.2f}s per page (sequential, main thread)")
    print()