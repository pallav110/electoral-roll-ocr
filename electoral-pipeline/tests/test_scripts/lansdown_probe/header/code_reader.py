"""Who owns the section CODE, and which engine can actually read it?

Ground truth: S24-53 prints section 1.  The Hindi read of its header line says
``अनुभाग संख्या और नाम : 4-उत्तरांचल...`` -- the leading digit reads as 4.

The module already documents this exact confusion, twice:

  * "Tesseract's Hindi model reads the printed numeral 1 as 4 on this roll, so
    its digit is unusable, but PaddleOCR reads the same pixels correctly."
  * "Numeric metadata therefore comes exclusively from the isolated,
    English-only high-resolution glyph reader below.  The broad header crop can
    blend the leading section glyph with adjacent text and return values such
    as ``14``/``113``."

So the Hindi read is a KNOWN-unsafe source for a digit, by design.  The located
band is read with lang="hin" -- to recognise the Devanagari LABEL -- and taking
the code from that same read reintroduces the confusion the English-only reader
was introduced to avoid.

This measures it: for each roll, locate the band, then read the leading glyph
three ways and compare.
"""
import sys

sys.stdout = open("/data/code_owner.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

TRUTH = {"/data/roll_s24_300.pdf": "1", "/data/roll107.pdf": "1"}
LABEL = {"/data/roll_s24_300.pdf": "S24-53", "/data/roll107.pdf": "S28-40 Lansdown"}

WHITELIST = ("--oem 3 --psm 7 "
             "-c tessedit_char_whitelist=0123456789-[]|!")

for path in TRUTH:
    truth = TRUTH[path]
    doc = fitz.open(path)
    page = doc[2]
    print("=" * 78)
    print(f"{LABEL[path]}   ground truth anubhag_code = {truth!r}")
    print("=" * 78)

    located = A._find_anubhag_header_band(page)
    if located is None:
        print("  band not located")
        continue
    rect, band_text = located
    print(f"  band at {rect.y0:.1f}-{rect.y1:.1f}pt")
    print(f"  hindi read  : {band_text!r}")
    print(f"  -> parsed   : {A._anubhag_code_from_header_text(A._split_anubhag_label(band_text))!r}")

    # The leading glyph is everything before the value's own dash.  Crop it
    # generously, then let the digit whitelist decide.
    high = A._render_region(page, rect, 1200)
    gray_full = cv2.cvtColor(high, cv2.COLOR_BGR2GRAY)
    dark = (gray_full < 140).sum(axis=0)
    cols = np.where(dark > 0)[0]
    if not len(cols):
        print("  no ink in band")
        continue
    left = cols[0]
    # The dash follows the digit; take the leading slice up to ~8% of the ink
    # width, which is wide enough for a digit plus a dash and narrow enough not
    # to reach the locality.
    span = cols[-1] - left
    prefix = gray_full[:, left:left + max(40, int(span * 0.08))]

    print(f"\n  leading-glyph crop {prefix.shape} "
          f"({max(40, int(span * 0.08))}px of {span}px ink span):")
    for name, img in (
        ("raw", prefix),
        ("otsu", cv2.threshold(prefix, 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]),
    ):
        for psm in (7, 13, 8):
            eng = " ".join(pytesseract.image_to_string(
                Image.fromarray(img), lang="eng",
                config=WHITELIST.replace("--psm 7", f"--psm {psm}")).split())
            hin = " ".join(pytesseract.image_to_string(
                Image.fromarray(img), lang="hin",
                config=f"--oem 3 --psm {psm}").split())
            mark = "  <== MATCHES TRUTH" if eng.strip()[:1] == truth else ""
            print(f"    {name:5} psm{psm:>2}  eng={eng!r:14} "
                  f"hin={hin[:18]!r}{mark}")

    # And what the module's own English-only fallback reader returns.
    print(f"\n  _extract_anubhag_code_from_header_fallback -> "
          f"{A._extract_anubhag_code_from_header_fallback(page)!r}")
    print(f"  _page_prints_section_header -> {A._page_prints_section_header(page)}")
    print()