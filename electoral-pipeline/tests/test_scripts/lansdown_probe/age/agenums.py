"""Are the age digits arriving as Devanagari numerals?

The extraction is `re.findall(r"\\d{1,3}", pt)`, which matches ASCII digits only.
This roll prints `आयु : 41` and the Tesseract reads of the age crop return
`4॥` -- the trailing `1` collapsing to a double danda -- so only a `4` is
extractable and it falls below AGE_MIN.

Two questions, measured rather than assumed:
  1. What does Paddle return for this crop -- ASCII digits, or Devanagari?
  2. Does normalising Devanagari numerals before extraction recover the values?

The normaliser already exists in the module (the section-code reader uses it), so
this checks whether it simply is not applied on this path.
"""
import sys

sys.stdout = open("/data/agenums.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import re
import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

DEVA_TO_ASCII = str.maketrans("०१२३४५६७८९", "0123456789")

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
paddle = A._get_paddle()

TRUTH = {0: "41", 1: "37", 3: "68", 4: "88"}

for idx, truth in TRUTH.items():
    r = rects[idx]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card
    crop = A._crop_card_region(clean, A.AGE_REGION, "AGE")
    if crop is None or getattr(crop, "size", 0) == 0:
        print(f"card {idx}: empty crop")
        continue

    # Scale it exactly as the stacked pass does, then read it alone.
    s = min(6, max(2, 80 // max(1, crop.shape[0])))
    small = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    try:
        res = paddle.ocr(small, cls=False)
    except Exception as exc:
        print(f"card {idx}: paddle failed: {exc}")
        continue
    texts = [ln[1][0].strip() for ln in (res[0] or []) if ln[1][0]]

    print("=" * 72)
    print(f"card {idx}  printed age = {truth}   crop {crop.shape} -> {small.shape}")
    print("=" * 72)
    print(f"  paddle texts        : {texts}")
    print(f"  shows age row       : {A._crop_shows_age_row(crop)}")

    # What the current expression extracts, ASCII-only.
    ascii_runs = []
    for pt in texts:
        ascii_runs += re.findall(r"\d{1,3}", pt)
    # What it would extract after normalising Devanagari numerals.
    norm_runs = []
    for pt in texts:
        norm_runs += re.findall(r"\d{1,3}", pt.translate(DEVA_TO_ASCII))

    ok_ascii = [d for d in ascii_runs if 18 <= int(d) <= 120]
    ok_norm = [d for d in norm_runs if 18 <= int(d) <= 120]
    print(f"  ascii-only runs     : {ascii_runs} -> in-range {ok_ascii}")
    print(f"  after normalisation : {norm_runs} -> in-range {ok_norm}")
    print(f"  {'MATCHES printed' if truth in ok_norm else 'still no match'}")
    print()