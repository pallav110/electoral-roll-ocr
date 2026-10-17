"""Locate the leading section digit by finding the dash that follows it.

The previous crop took the band's left edge, which is the LABEL ('अनु+'), not
the value.  The digit sits between the end of the label and the value's own dash,
so the crop has to be anchored on the dash.

A printed dash is a wide, thin ink run.  Finding it by shape rather than by
fraction avoids the same class of error the rest of this work is fixing: the
dash's position inside the band differs between templates, but its shape does
not.  Once the dash is known, the digit is the ink immediately to its left, with
the label's own ink excluded by the gap between them.
"""
import sys

sys.stdout = open("/data/digit_find.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

TRUTH = {"/data/roll_s24_300.pdf": ("S24-53", "1"),
         "/data/roll107.pdf": ("S28-40 Lansdown", "1")}
WHITELIST = "-c tessedit_char_whitelist=0123456789"


def find_dash_columns(gray):
    """Column ranges holding a wide-thin horizontal rule (a dash)."""
    h, w = gray.shape
    dark = gray < 140
    out = []
    for x in range(w):
        col = dark[:, x]
        rows = np.where(col)[0]
        if not len(rows):
            continue
        top, bottom = rows[0], rows[-1]
        span = bottom - top + 1
        # A dash: occupies a small fraction of the band height, yet its run of
        # dark pixels is nearly contiguous and reasonably wide.
        if span <= max(3, h * 0.25) and (bottom - top) < h * 0.3:
            filled = col[top:bottom + 1].sum() / span
            if filled > 0.85:
                out.append((x, top, bottom))
    # Group adjacent columns into one dash.
    groups = []
    for x, top, bottom in out:
        if groups and x - groups[-1][-1][0] <= 6:
            groups[-1].append((x, top, bottom))
        else:
            groups.append([(x, top, bottom)])
    return [(g[0][0], g[-1][0], min(t for _, t, _ in g), max(b for _, _, b in g))
            for g in groups]


for path, (label, truth) in TRUTH.items():
    doc = fitz.open(path)
    page = doc[2]
    print("=" * 78)
    print(f"{label}   truth {truth!r}")
    print("=" * 78)
    located = A._find_anubhag_header_band(page)
    rect, band_text = located
    print(f"  band {rect.y0:.1f}-{rect.y1:.1f}pt   hindi: {band_text[:60]!r}")

    high = A._render_region(page, rect, 600)
    gray = cv2.cvtColor(high, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    print(f"  rendered {w}x{h}px")

    dashes = find_dash_columns(gray)
    print(f"  dash candidates (x0,x1,top,bottom): {dashes[:6]}")

    dark = (gray < 140).sum(axis=0)
    inked = np.where(dark > 0)[0]
    if not dashes and not len(inked):
        continue

    # Prefer the FIRST dash that is preceded by ink and followed by ink: the
    # value's dash has a digit before it and a locality after it.
    chosen = None
    for x0, x1, top, bottom in dashes:
        left_gap = np.where(dark[:x0] == 0)[0]
        if not len(left_gap):
            continue
        gap_start = left_gap[-1]
        if x0 - gap_start < 4:
            continue           # no whitespace before it -> not the value dash
        chosen = (x0, x1, gap_start)
        break
    if chosen is None and dashes:
        chosen = (dashes[0][0], dashes[0][1], 0)
    if chosen is None:
        print("  no dash found")
        continue

    x0, x1, gap_start = chosen
    print(f"  chosen dash x={x0}-{x1}, whitespace before it starts at {gap_start}")
    # The digit is the ink between the last inked column before the gap and x0.
    before = np.where(dark[:x0] > 0)[0]
    digit_x1 = before[-1] if len(before) else x0
    pad = max(4, (x1 - x0) // 2)
    crop = gray[:, max(0, digit_x1 - pad):x0 + pad]
    print(f"  digit crop {crop.shape} (cols {max(0, digit_x1 - pad)}..{x0 + pad})"
          f"  band was {w}px wide")

    for name, img in (("raw", crop),
                      ("otsu", cv2.threshold(crop, 0, 255,
                                             cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])):
        for psm in (7, 8, 13):
            eng = " ".join(pytesseract.image_to_string(
                Image.fromarray(img), lang="eng",
                config=f"--oem 3 --psm {psm} {WHITELIST}").split())
            got = eng.strip()[:1]
            mark = "  <== TRUTH" if got == truth else ""
            print(f"    {name:5} psm{psm:>2} -> {eng!r}{mark}")
    print()