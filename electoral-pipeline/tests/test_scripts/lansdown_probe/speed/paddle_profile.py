"""Attribute the Paddle cost per page, stage by stage.

CLAUDE.md says the deleted-mark vote is "8 of the pipeline's 9 Paddle calls per
card" and "the single most expensive OCR operation in the pipeline".  The vote
already carries an exact early exit, so that claim may be stale.  This times
each Paddle stage separately on a real page so the fix targets the stage that
actually costs, rather than the one the docs name.
"""
import sys
import time

sys.stdout = open("/data/paddle_prof.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

PATH = "/data/roll107.pdf"
PAGE = 3                      # 0-based -> printed page 4
SCALE = 200 / 72.0

doc = fitz.open(PATH)
page = doc[PAGE]
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
rects = A._voter_card_rects(page)
cards = [
    raw[int(r.y0 * SCALE):int(r.y1 * SCALE), int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    for r in rects
]
print(f"page {PAGE + 1}: {len(cards)} card positions, "
      f"cards {cards[0].shape} to {cards[-1].shape}")

paddle = A._get_paddle()
stages = {}


def timed(name, fn, *a, **kw):
    t = time.perf_counter()
    try:
        return fn(*a, **kw)
    finally:
        stages[name] = stages.get(name, 0.0) + time.perf_counter() - t


# --- 1. the deleted-mark vote, card by card (the documented suspect) --------
t = time.perf_counter()
verdicts, reader_counts = [], []
for card in cards:
    serial = A._crop_card_region(card, A.TESSERACT_CARD_CONTENT_REGIONS[0], "SERIAL")
    if serial is None or getattr(serial, "size", 0) == 0:
        verdicts.append((False, 0, 0))
        continue
    verdicts.append(A._serial_deleted_vote(serial))
vote_s = time.perf_counter() - t
reader_counts = [v[2] for v in verdicts]
hits = [v[1] for v in verdicts]
print(f"\nDELETED VOTE   {vote_s:7.2f}s total, {vote_s / len(cards):5.3f}s/card")
print(f"  readers run per card: min {min(reader_counts)} "
      f"max {max(reader_counts)} mean {sum(reader_counts) / len(reader_counts):.2f}")
print(f"  hits per card:        {sorted(set(hits))}")
print(f"  cards marked deleted: {sum(1 for v in verdicts if v[0])}")

# --- 2. the stacked serial+house+age pass, once per card -------------------
t = time.perf_counter()
for card in cards:
    clean = A._remove_outer_card_lines(card)
    stacked = np.vstack([clean] * 3) if clean is not None else None
    if stacked is None:
        continue
    try:
        paddle.ocr(stacked, cls=False)
    except Exception:
        pass
stacked_s = time.perf_counter() - t
print(f"\nSTACKED PASS   {stacked_s:7.2f}s total, {stacked_s / len(cards):5.3f}s/card")

# --- 3. the Tesseract pass, for reference (it parallelises) -----------------
t = time.perf_counter()
for card in cards:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))
    A._extract_text_with_tesseract(buf.tobytes())
tess_serial_s = time.perf_counter() - t
print(f"TESSERACT (serial, serial) {tess_serial_s:6.2f}s total, "
      f"{tess_serial_s / len(cards):5.3f}s/card  [runs on 14 workers]")

total = vote_s + stacked_s
print("\n" + "=" * 78)
print(f"Paddle total (sequential): {total:6.2f}s/page")
print(f"  deleted vote: {vote_s:6.2f}s ({vote_s / total * 100:.0f}%)")
print(f"  stacked pass: {stacked_s:6.2f}s ({stacked_s / total * 100:.0f}%)")
print(f"Tesseract serial-equivalent: {tess_serial_s / 14:6.2f}s/page on 14 workers")
print(f"Target: 7-9s/page end to end.")
print("=" * 78)