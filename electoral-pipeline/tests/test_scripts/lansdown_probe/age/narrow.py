"""Does `_age_paddle_crop` actually isolate the age, and does Paddle then read it?

Three things are checked per card and printed:
  * whether `_age_value_span` located the labels at all (it returns None when it
    cannot, and the crop is then passed through unchanged);
  * the narrowed crop's shape against the wide one, and what Paddle returns from
    each, so the welded digits ('337' for a printed 37) can be compared directly.
"""
import sys

sys.stdout = open("/data/narrow.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import re
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
paddle = A._get_paddle()
TRUTH = {0: "41", 1: "37", 3: "68", 4: "88", 5: "79", 6: "48"}


def read(img):
    s = min(6, max(2, 80 // max(1, img.shape[0])))
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    try:
        res = paddle.ocr(small, cls=False)
    except Exception as exc:
        return [f"<error {exc}>"]
    return [ln[1][0].strip() for ln in (res[0] or []) if ln[1][0]]


for idx, truth in TRUTH.items():
    r = rects[idx]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card
    wide = A._crop_card_region(clean, A.AGE_REGION, "AGE")
    if wide is None or getattr(wide, "size", 0) == 0:
        print(f"card {idx}: empty crop")
        continue

    gray = cv2.cvtColor(wide, cv2.COLOR_BGR2GRAY)
    span = A._age_value_span(gray)
    narrow = A._age_paddle_crop(wide)

    print("=" * 72)
    print(f"card {idx}  printed age = {truth}")
    print("=" * 72)
    print(f"  wide crop        {wide.shape}   label found: {span is not None}")
    print(f"  span found       {None if span is None else span.shape}")
    print(f"  narrowed crop    {narrow.shape}")
    print(f"  WIDE paddle      {read(wide)}")
    print(f"  NARROW paddle    {read(narrow)}")
    cv2.imwrite(f"/data/nw{idx}_wide.png", cv2.cvtColor(wide, cv2.COLOR_RGB2BGR))
    cv2.imwrite(f"/data/nw{idx}_narrow.png", cv2.cvtColor(narrow, cv2.COLOR_RGB2BGR))
    print()