"""Why does the locator return None on a synthetic page?  Measure, don't guess.

Walks the locator's own steps on a generated page and prints what each stage
produces: the render, the band grouping, and the OCR read of every band.  The
question is which stage drops the labelled band -- the strip, the grouping, the
minimum band height, or the OCR itself.
"""
import sys

sys.stdout = open("/data/locator_dbg.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

PAGE_PT = 1190.0
LINE_PT = 30.0

doc = fitz.open()
page = doc.new_page(width=PAGE_PT * 0.707, height=PAGE_PT)
page.insert_text((28, LINE_PT + 12), "SECTIONLINE 4-TESTCOLONY",
                 fontsize=13, fontname="helv")
for i in range(2):
    page.insert_text((28, LINE_PT * 0.4 + i * 9), "Vidhan Sabha kshetra 40",
                     fontsize=11, fontname="helv")
    page.insert_text((28, PAGE_PT * 0.5 + i * 14), f"voter card row {i}",
                     fontsize=11, fontname="helv")
doc.save("/data/synth.pdf")
doc.close()

doc = fitz.open("/data/synth.pdf")
page = doc[0]
H = page.rect.height
print(f"page {page.rect.width:.1f}x{H:.1f}pt   line drawn at y={LINE_PT}")

# stage 1: the render the locator performs
clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                 page.rect.y0 + H * A._ANUBHAG_SCAN_TOP_FRACTION)
print(f"scan fraction = {A._ANUBHAG_SCAN_TOP_FRACTION}  ->  "
      f"clip height = {A._ANUBHAG_SCAN_TOP_FRACTION * H:.1f}pt")
raw = page.get_pixmap(dpi=200, clip=clip, alpha=False).tobytes("png")
print(f"pixmap bytes = {len(raw)}")
image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
print(f"imdecode -> {None if image is None else image.shape}")
if image is None:
    raise SystemExit(1)

gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
strip_px = gray.shape[0]
print(f"strip = {gray.shape[1]}x{strip_px}px   "
      f"line at {LINE_PT}pt = row {LINE_PT / H * strip_px:.0f}")

# how much ink is in the strip at all?
for thresh in (140, 180, 220, 250):
    dark = (gray < thresh).sum(axis=1)
    rows = np.where(dark > 2)[0]
    print(f"  thresh {thresh}: {len(rows)} rows with ink, "
          f"max dark in a row = {int(dark.max())}")

# stage 2: band grouping, at the thresholds the locator uses
dark = (gray < 140).sum(axis=1)
rows = np.where(dark > 2)[0]
gap = max(4, strip_px // 100)
min_band = max(2, strip_px // 200)
print(f"\ngap = {gap}   min_band = {min_band}   (strip {strip_px}px)")
if not len(rows):
    print("  NO ROWS WITH INK -- the strip is blank at this render")
else:
    groups = [[int(rows[0])]]
    for r in rows[1:]:
        r = int(r)
        if r - groups[-1][-1] <= gap:
            groups[-1].append(r)
        else:
            groups.append([r])
    for g in groups:
        y0, y1 = g[0], g[-1]
        kept = y1 - y0 >= min_band
        band = gray[y0:y1 + 1, :]
        cols = np.where((band < 140).sum(axis=0) > 0)[0]
        crop = band[:, cols[0]:cols[-1] + 1] if len(cols) else band
        scale = max(1, -(-A.TESSERACT_MIN_HEIGHT_PX // max(1, crop.shape[0])))
        up = cv2.resize(crop, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_CUBIC) if scale > 1 else crop
        text = " ".join(pytesseract.image_to_string(
            Image.fromarray(up), lang="hin", config="--oem 3 --psm 7").split())
        en = " ".join(pytesseract.image_to_string(
            Image.fromarray(up), lang="eng", config="--oem 3 --psm 7").split())
        print(f"  rows {y0:>4}-{y1:>4} h={y1-y0+1:>3} kept={kept} "
              f"x{scale}  hin={text[:46]!r}")
        print(f"                          eng={en[:46]!r}")

# the real answer: does the locator find it with the injected query?
A._ANUBHAG_BAND_LABEL = "SECTIONLINE"
print(f"\nlocator with injected query -> {A._find_anubhag_header_band(page)}")
A._ANUBHAG_BAND_LABEL = "अनुभाग"
print(f"locator with real Devanagari query -> "
      f"{A._find_anubhag_header_band(page)}")