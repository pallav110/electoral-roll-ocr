"""Measure the real row gaps the band grouper has to separate.

The synthetic rows are drawn at page-height fractions, matching the measured
real layouts.  This prints the actual blank-row runs between them at every page
scale, on both the synthetic pages and the two real rolls, so the gap threshold
is chosen from what actually separates two printed lines rather than guessed.
"""
import sys

sys.stdout = open("/data/gap_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A


def strip_gray(page):
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + page.rect.height * A._ANUBHAG_SCAN_TOP_FRACTION)
    raw = page.get_pixmap(dpi=200, clip=clip, alpha=False).tobytes("png")
    return cv2.cvtColor(cv2.imdecode(np.frombuffer(raw, np.uint8),
                                     cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)


def blank_runs(gray):
    """Every maximal run of rows carrying no ink, longer than one row."""
    dark = (gray < 140).sum(axis=1)
    blank = dark <= 2
    runs, start = [], None
    for y, is_blank in enumerate(blank):
        if is_blank and start is None:
            start = y
        elif not is_blank and start is not None:
            runs.append((start, y - start))
            start = None
    if start is not None:
        runs.append((start, len(blank) - start))
    return [(s, n) for s, n in runs if n >= 2]


print("=" * 78)
print("SYNTHETIC pages -- rows at page-height fractions, as on a real header")
print("=" * 78)
for page_pt in (842.0, 1190.0, 2806.0):
    doc = fitz.open()
    page = doc.new_page(width=page_pt * 0.707, height=page_pt)
    page.insert_text((28, page_pt * 0.012 + 12), "Vidhan Sabha kshetra sankhya",
                     fontsize=13, fontname="helv")
    page.insert_text((28, page_pt * 0.026 + 12), "Anubhag sankhya aur naam",
                     fontsize=13, fontname="helv")
    p = f"/data/gap_{page_pt:.0f}.pdf"
    doc.save(p)
    doc.close()
    d = fitz.open(p)
    g = strip_gray(d[0])
    strip_px = g.shape[0]
    gaps = blank_runs(g)
    print(f"\n  page {page_pt:>6.0f}pt -> strip {g.shape[1]}x{strip_px}px  "
          f"gap = {max(4, strip_px//100)}  min_band = {max(2, strip_px//200)}")
    for start, n in gaps:
        print(f"    blank run rows {start:>4}-{start+n-1:<4} ({n:>3}px)")
    print(f"    bands found: {A._header_ink_bands(g)}")
    d.close()

print("\n" + "=" * 78)
print("REAL rolls -- the gaps that separate the printed header rows")
print("=" * 78)
for label, path in (("S28-40 Lansdown", "/data/roll107.pdf"),
                    ("S24-53", "/data/roll_s24_300.pdf")):
    d = fitz.open(path)
    page = d[2]
    g = strip_gray(page)
    strip_px = g.shape[0]
    print(f"\n  {label}  page {page.rect.width:.0f}x{page.rect.height:.0f}pt "
          f"-> strip {g.shape[1]}x{strip_px}px  gap = {max(4, strip_px//100)}")
    for start, n in blank_runs(g):
        print(f"    blank run rows {start:>4}-{start+n-1:<4} ({n:>3}px)")
    print(f"    bands found: {A._header_ink_bands(g)}")
    d.close()