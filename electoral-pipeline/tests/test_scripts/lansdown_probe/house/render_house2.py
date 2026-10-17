"""Render the REAL house-number crops so the disputed values can be read.

The previous probe used TESSERACT_CARD_CONTENT_REGIONS[1], which is
(0.52, 0.02, 1.0, 0.23) -- the photo box, top right of the card.  The house
number lives in HOUSE_REGION, (0.03, 0.45, 0.62, 0.59).  Rendering the wrong
region explains the phantom constant '7': nothing in a photo box is a 7, but a
border line reads as one, and it read the same on every card.

This writes HOUSE_REGION for the first 8 cards, plain and thresholded, plus the
whole card with the region boxed, and prints what each reader returns so the
image and the number can be compared directly.
"""
import sys

sys.stdout = open("/data/render2.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
N = 8

doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]

print(f"HOUSE_REGION = {A.HOUSE_REGION}")
print(f"card {raw[int(rects[0].y0*SCALE):int(rects[0].y1*SCALE), int(rects[0].x0*SCALE):int(rects[0].x1*SCALE)].shape}")

for idx in range(N):
    card = raw[int(rects[idx].y0 * SCALE):int(rects[idx].y1 * SCALE),
               int(rects[idx].x0 * SCALE):int(rects[idx].x1 * SCALE)].copy()
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card

    crop = A._crop_card_region(clean, A.HOUSE_REGION, "HOUSE")
    if crop is None or getattr(crop, "size", 0) == 0:
        print(f"  card {idx}: empty crop")
        continue

    big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cv2.imwrite(f"/data/hh{idx}_house.png", np.vstack([gray, otsu]))

    box = clean.copy()
    h, w = clean.shape[:2]
    cv2.rectangle(box,
                  (int(w * A.HOUSE_REGION[0]), int(h * A.HOUSE_REGION[1])),
                  (int(w * A.HOUSE_REGION[2]), int(h * A.HOUSE_REGION[3])),
                  (0, 0, 255), 3)
    cv2.putText(box, str(idx), (12, 46), cv2.FONT_HERSHEY_SIMPLEX, 1.6,
                (0, 0, 255), 3)
    cv2.imwrite(f"/data/hh{idx}_card.png", cv2.cvtColor(box, cv2.COLOR_RGB2BGR))

    rec = A._extract_card(page, rects[idx], 4, idx)
    print(f"  card {idx}: house_no={rec.get('house_no') if rec else None!r:>8}  "
          f"wrote hh{idx}_card.png / hh{idx}_house.png")

print("\ndone")