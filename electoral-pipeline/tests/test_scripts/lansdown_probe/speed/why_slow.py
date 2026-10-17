"""Isolating tesserocr: 0.18s per GetUTF8Text on ONE card.

A single card read takes 0.18s.  Six and a half reads per card, 30 cards, is
~35s sequential -- which matches the 97.69s measured for the full page once the
other reads are added.  So the question is not "why don't threads scale" (they
do: 6.8x on this exact call) but "why does one 689x1761 card cost 0.18s when
the same engine reads a small crop in 0.008s".

The answer is almost certainly pixel count, not the API.  This measures cost
against image area by downscaling the same card, holding everything else fixed.
If cost tracks area, the fix is to stop OCRing pixels nobody reads -- which is
Unit 1 of the design, and the numbers here are the justification for it.
"""
import sys
import time

sys.stdout = open("/data/why_slow.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
from tesserocr import PyTessBaseAPI
import ocr_pdf_api as A

SCALE = 200 / 72.0


def card_of(path, page_no):
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    r = rects[0]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    doc.close()
    return card


LANSDOWN = card_of("/data/roll107.pdf", 3)
S24 = card_of("/data/roll_s24_300.pdf", 3)


def timed_read(img, lang="hin+eng", psm=6, n=4):
    api = PyTessBaseAPI(path="", lang=lang)
    api.SetImage(img)
    api.SetSourceResolution(300)
    api.SetPageSegMode(tesserocr_psm(psm))
    api.SetImage(img)
    api.GetUTF8Text()                      # warm
    t = time.perf_counter()
    for _ in range(n):
        api.SetImage(img)
        api.GetUTF8Text()
    el = (time.perf_counter() - t) / n
    del api
    return el


def tesserocr_psm(psm):
    from tesserocr import PSM
    return {6: PSM.SINGLE_BLOCK, 7: PSM.SINGLE_LINE, 3: PSM.AUTO}.get(psm, PSM.AUTO)


print("=" * 74)
print("COST vs IMAGE SIZE  (lang=hin+eng, psm 6, one warm engine)")
print("=" * 74)
print(f"{'source':<12} {'W x H':>12} {'megapixels':>11} {'seconds':>9}")
print("-" * 48)
for name, card in (("Lansdown", LANSDOWN), ("S24-53", S24)):
    g = cv2.cvtColor(card, cv2.COLOR_RGB2GRAY)
    el = timed_read(Image.fromarray(g))
    mp = g.shape[0] * g.shape[1] / 1e6
    print(f"{name:<12} {g.shape[1]:>5} x{g.shape[0]:>4} {mp:>11.2f} {el:>9.4f}")

print("\n" + "=" * 74)
print("THE SAME CARD, downscaled -- does cost track pixels?")
print("=" * 74)
base_g = cv2.cvtColor(LANSDOWN, cv2.COLOR_RGB2GRAY)
full = timed_read(Image.fromarray(base_g))
print(f"{'scale':>7} {'W x H':>12} {'megapixels':>11} {'seconds':>9} "
      f"{'vs full':>9}")
print("-" * 62)
for factor in (1.0, 0.75, 0.5, 0.35, 0.25):
    if factor == 1.0:
        g = base_g
    else:
        g = cv2.resize(base_g, None, fx=factor, fy=factor,
                       interpolation=cv2.INTER_AREA)
    el = timed_read(Image.fromarray(g))
    mp = g.shape[0] * g.shape[1] / 1e6
    print(f"{factor:>7.2f} {g.shape[1]:>5} x{g.shape[0]:>4} {mp:>11.2f} "
          f"{el:>9.4f} {el / full:>8.2f}x")

print("\n" + "=" * 74)
print("HOW MUCH TEXT IS ACTUALLY IN THE CARD?")
print("=" * 74)
for name, card in (("Lansdown", LANSDOWN), ("S24-53", S24)):
    g = cv2.cvtColor(card, cv2.COLOR_RGB2GRAY)
    dark = (g < 140)
    rows = np.where(dark.sum(axis=1) > 2)[0]
    bands, cur = [], [rows[0]]
    for rr in rows[1:]:
        if rr - cur[-1] <= 8:
            cur.append(rr)
        else:
            bands.append(cur)
            cur = [rr]
    bands.append(cur)
    heights = [b[-1] - b[0] + 1 for b in bands]
    print(f"  {name}: {g.shape[1]}x{g.shape[0]}, {len(bands)} text bands, "
          f"median band height {int(np.median(heights))}px")
    print(f"      ink coverage {dark.mean() * 100:.2f}% of the card")

print("\n" + "=" * 74)
print("WHAT THE PIPELINE ACTUALLY READS PER CARD")
print("=" * 74)
import inspect
src = inspect.getsource(A._extract_card)
for needle in ("_tess_data(", "_tess_string(",
               "_extract_house_number_with_tesseract",
               "_extract_serial_with_tesseract", "_extract_epic_with_tesseract"):
    print(f"  {src.count(needle):>3}x  {needle}")
print(f"  _extract_card source length: {len(src)} chars")