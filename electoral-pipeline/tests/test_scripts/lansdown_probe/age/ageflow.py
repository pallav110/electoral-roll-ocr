"""Trace card 0's age end to end, after the AGE_REGION widening.

The region now starts at 0.51 and covers the printed row, yet `_extract_card`
still returns '' for the age while returning 37, 68 and 33 for its neighbours.
So the region is no longer the blocker and the fault is further down: either the
Hindi reader never produced an age, or the value was dropped afterwards.

Every stage is printed for card 0 and for a card that works, side by side.
"""
import sys

sys.stdout = open("/data/ageflow.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]

print(f"AGE_REGION = {A.AGE_REGION}")
print(f"GENDER_AGE_REGION = {A.GENDER_AGE_REGION}\n")

for idx in (0, 1, 3):
    r = rects[idx]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    h, w = card.shape[:2]
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card

    print("=" * 70)
    print(f"card {idx}   card {w}x{h}")
    print("=" * 70)

    # 1. the Hindi age reader on the whole clean card
    focused = A._extract_age_from_clean_card(clean)
    print(f"  _extract_age_from_clean_card      = {focused!r}")

    # 2. the AGE_REGION crop, and its ink fraction
    age_crop = A._crop_card_region(clean, A.AGE_REGION, "AGE")
    if age_crop is not None and getattr(age_crop, "size", 0):
        gray = age_crop if len(age_crop.shape) == 2 else cv2.cvtColor(age_crop, cv2.COLOR_BGR2GRAY)
        print(f"  AGE_REGION crop                   = {age_crop.shape}  "
              f"ink={float((gray < 140).mean()):.4f}  "
              f"has_ink={A._crop_has_ink(age_crop)}")
        cv2.imwrite(f"/data/af{idx}_agecrop.png",
                    cv2.cvtColor(age_crop, cv2.COLOR_RGB2BGR))
    else:
        print("  AGE_REGION crop EMPTY")

    # 3. what the Hindi line parse produced, which is where age usually comes from
    ok, buf = cv2.imencode(".png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))
    lines, conf = A._extract_text_with_tesseract(buf.tobytes())
    rec_parse = A.parse_voter_box_from_ocr_lines(lines or [])
    print(f"  parse_voter_box_from_ocr_lines age = {rec_parse.get('age')!r}")
    print(f"  first 2 lines: {(lines or [])[:2]}")

    # 4. the final record
    rec = A._extract_card(page, r, 4, idx) or {}
    print(f"  FINAL age                         = {rec.get('age')!r}")
    print(f"  FINAL review keys                 = "
          f"{[k for k in rec if 'review' in k.lower()]}")
    print(f"  FINAL age-related flags           = "
          f"{ {k: v for k, v in rec.items() if 'age' in k.lower()} }")
    print()