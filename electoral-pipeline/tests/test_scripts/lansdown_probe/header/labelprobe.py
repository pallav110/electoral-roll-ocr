"""Why does `_crop_shows_age_row` reject a crop that plainly shows आयु : 41?

The crop rendered by AGE_REGION = (0.03, 0.51, 0.55, 0.78) contains both
`मकान संख्या : 7` and `आयु : 41 लिंग : महिला`, sharply.  The guard still returns
False, so the age is dropped.  The image is correct, which means the PROBE is
wrong -- and a probe that cannot see a label it is looking for will silently
blank every age on every roll.

Each step is printed: the ink test, the upscale actually applied, and the text
Tesseract returns at each page-segmentation mode, so the failing step is named.
"""
import sys

sys.stdout = open("/data/labelprobe.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]

print(f"AGE_REGION        = {A.AGE_REGION}")
print(f"TESSERACT_MIN_HEIGHT_PX = {A.TESSERACT_MIN_HEIGHT_PX}")
print(f"AGE_LABEL_RE      = {A.AGE_LABEL_RE.pattern}\n")

for idx in (0, 1, 3):
    r = rects[idx]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card
    crop = A._crop_card_region(clean, A.AGE_REGION, "AGE")
    print("=" * 72)
    print(f"card {idx}: crop {crop.shape if crop is not None else None}")
    print("=" * 72)
    if crop is None or getattr(crop, "size", 0) == 0:
        continue

    print(f"  _crop_has_ink      = {A._crop_has_ink(crop)}")
    print(f"  _crop_shows_age_row= {A._crop_shows_age_row(crop)}")

    gray = crop if len(crop.shape) == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scale = max(1, -(-A.TESSERACT_MIN_HEIGHT_PX // max(1, gray.shape[0])))
    print(f"  crop height {gray.shape[0]}px -> scale {scale} "
          f"(needs {A.TESSERACT_MIN_HEIGHT_PX})")
    up = (cv2.resize(gray, None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_CUBIC) if scale > 1 else gray)
    print(f"  reading at {up.shape[1]}x{up.shape[0]}px")

    for psm in (6, 7, 4, 11):
        for lang in ("hin", "hin+eng"):
            txt = " ".join(pytesseract.image_to_string(
                Image.fromarray(up), lang=lang,
                config=f"--oem 3 --psm {psm}").split())
            hit = bool(A.AGE_LABEL_RE.search(txt))
            print(f"    psm{psm:>2} {lang:8} label={'YES' if hit else 'no ':>3}  "
                  f"{txt[:56]!r}")
    print()