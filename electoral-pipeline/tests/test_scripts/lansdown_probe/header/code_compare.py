"""Compare every way this codebase can read the section digit, side by side.

Three prior attempts anchored on geometry -- a fixed left fraction, then a
dash found by shape -- and both picked up glyph parts of the Devanagari label
instead of the digit.  Detecting "which glyph is the value" from pixels is a
harder problem than it looks, and it is not what this fix needs.

What this does instead: read the whole band with each engine at each resolution
and print every answer, so the safe owner of a digit is chosen from measurement
rather than from a fourth guess about where the digit sits.

The question is narrow.  Ground truth is section 1 on both rolls.  The Hindi
read of S24 says '4', which is the confusion the module already documents:
"Tesseract's Hindi model reads the printed numeral 1 as 4 on this roll".
"""
import sys

sys.stdout = open("/data/code_compare.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

TRUTH = {"/data/roll_s24_300.pdf": ("S24-53", "1"),
         "/data/roll107.pdf": ("S28-40 Lansdown", "1")}
WL = "-c tessedit_char_whitelist=0123456789-[]|!ह"

for path, (label, truth) in TRUTH.items():
    doc = fitz.open(path)
    page = doc[2]
    rect, band_text = A._find_anubhag_header_band(page)
    print("=" * 78)
    print(f"{label}   truth {truth!r}   band {rect.y0:.1f}-{rect.y1:.1f}pt")
    print("=" * 78)
    print(f"  located hindi read : {band_text[:64]!r}")
    print(f"  located parsed     : "
          f"{A._anubhag_code_from_header_text(A._split_anubhag_label(band_text))!r}")

    print(f"\n  whole-band reads (truth {truth!r}):")
    for dpi in (300, 600, 1200):
        img = A._render_region(page, rect, dpi)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        otsu = cv2.threshold(gray, 0, 255,
                             cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        for gname, g in (("gray", gray), ("otsu", otsu)):
            for psm in (6, 7):
                eng = " ".join(pytesseract.image_to_string(
                    Image.fromarray(g), lang="eng",
                    config=f"--oem 3 --psm {psm} {WL}").split())
                hit = eng.strip()[:1]
                mark = "  <== TRUTH" if hit == truth else ""
                print(f"    {dpi:>4}dpi {gname} psm{psm} eng={eng[:26]!r}{mark}")

    # The full top strip, which is what the English-only fallback reads.  Wider
    # context is what lets pytesseract segment the digit away from the label.
    for pct in (0.03, 0.05, 0.09):
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                         page.rect.y0 + page.rect.height * pct)
        img = A._render_region(page, clip, 300)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        eng = " ".join(pytesseract.image_to_string(
            Image.fromarray(gray), lang="eng",
            config=f"--oem 3 --psm 6 {WL}").split())
        hit = eng.strip()[:1]
        mark = "  <== TRUTH" if hit == truth else ""
        print(f"    strip {pct:.0%} psm6 eng={eng[:30]!r}{mark}")

    print(f"\n  module readers:")
    print(f"    header_fallback        -> "
          f"{A._extract_anubhag_code_from_header_fallback(page)!r}")
    print(f"    paddle digit runs      -> "
          f"{A._anubhag_digit_runs_via_paddle(page)!r}")
    print()