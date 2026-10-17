"""Render the disputed crops so they can be read by eye, not argued about.

Verification reported `house_no` differing on 8 of 10 Lansdown cards, with the
OLD path returning '7' on all eight.  A constant value across eight different
cards is more likely a card border being read as a digit than eight voters in
house 7, but that is a hypothesis and the way to settle it is to look at the
pixels.

This writes, for the first 6 cards:
  <n>_card.png      the whole card, with the house-number region boxed
  <n>_house.png     that region alone, both raw and thresholded

so the printed house number can be read directly.
"""
import sys

sys.stdout = open("/data/render.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
N = 6

doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)

print("house-number region index in TESSERACT_CARD_CONTENT_REGIONS:")
for i, r in enumerate(A.TESSERACT_CARD_CONTENT_REGIONS):
    print(f"  [{i}] {r}")

pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]

# Which region is the house number?  Find it by name.
region = None
for r in A.TESSERACT_CARD_CONTENT_REGIONS:
    if "house" in str(r[0]).lower():
        region = r
        break
if region is None:
    print("NO house-named region; using index 1")
    region = A.TESSERACT_CARD_CONTENT_REGIONS[1]
print(f"\nusing region {region}")

for idx in range(N):
    card = raw[int(rects[idx].y0 * SCALE):int(rects[idx].y1 * SCALE),
               int(rects[idx].x0 * SCALE):int(rects[idx].x1 * SCALE)].copy()
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card

    crop = A._crop_card_region(clean, region, "HOUSE")
    if crop is None or getattr(crop, "size", 0) == 0:
        print(f"  card {idx}: house crop empty")
        continue

    # Draw the region on the card so its location is unambiguous.
    box = clean.copy()
    h, w = clean.shape[:2]
    fh, fw = region[2], region[3]
    x0, y0 = int(w * fh), int(h * fw)
    x1, y1 = int(w * region[4]), int(h * region[5])
    cv2.rectangle(box, (x0, y0), (x1, y1), (0, 0, 255), 4)
    cv2.putText(box, f"card {idx}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX,
                1.4, (0, 0, 255), 3)
    cv2.imwrite(f"/data/h{idx}_card.png", cv2.cvtColor(box, cv2.COLOR_RGB2BGR))

    # The crop, upscaled for legibility, plain and thresholded side by side.
    big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    pair = np.vstack([gray, otsu])
    cv2.imwrite(f"/data/h{idx}_house.png", pair)

    rec = A._extract_card(page, rects[idx], 4, idx)
    print(f"  card {idx}: crop {crop.shape} -> /data/h{idx}_card.png "
          f"+ h{idx}_house.png   record house_no="
          f"{rec.get('house_no') if rec else None!r}")

print("\nwrote PNGs")