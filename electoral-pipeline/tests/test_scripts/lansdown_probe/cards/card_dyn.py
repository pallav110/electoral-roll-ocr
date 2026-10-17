"""Can card boundaries be FOUND rather than assumed?

The card grid is currently 3 x 10 positions from absolute fractional constants
(`VOTER_CARD_ROW_BOUNDS`, LEFT_MARGIN 0.011, RIGHT_MARGIN 0.019, COLUMN_GAP
0.0055).  Those were measured on one template, so they carry the same risk as
the header clips did -- and unlike the header they are load-bearing for every
single record.

This measures whether the gutters are actually detectable.  Cards are printed
boxes separated by white space, so both projections should show a clean
periodic structure:

  * row ink profile -- inside a card row, 3 cards sit side by side and every row
    carries ink; between card rows all three end at once, so ink drops to ~0.
  * column ink profile -- summed down all 10 rows, a card column carries ink
    from 10 card borders, and the gutter between columns carries almost none.

If that structure is as clean as the reasoning says, detection is feasible and
the constants become a fallback.  If it is noisy, detection is not reliable and
the honest answer is that cards have to stay geometric.
"""
import sys

sys.stdout = open("/data/card_dyn.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

CASES = (
    ("S28-40 Lansdown", "/data/roll107.pdf"),
    ("S24-53 works", "/data/roll_s24_300.pdf"),
)


def zero_runs(values: np.ndarray, limit: int) -> list[tuple[int, int]]:
    """Maximal runs of rows/cols whose ink is essentially nil."""
    empty = values <= limit
    runs: list[list[int]] = []
    for index, is_empty in enumerate(empty):
        if is_empty and (not runs or index != runs[-1][-1] + 1):
            runs.append([index])
        elif is_empty:
            runs[-1].append(index)
    return [(r[0], r[-1]) for r in runs]


for label, path in CASES:
    doc = fitz.open(path)
    page = doc[2]
    W, H = page.rect.width, page.rect.height
    print("=" * 78)
    print(f"{label}   page {W:.0f}x{H:.0f}pt")
    print("=" * 78)

    raw = page.get_pixmap(dpi=200, alpha=False)
    img = np.frombuffer(raw.samples, np.uint8).reshape(
        raw.height, raw.width, raw.n)[:, :, :3]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    ph, pw = gray.shape
    print(f"  render {pw}x{ph}px   page {W:.0f}x{H:.0f}pt")

    # A card border and text are dark; a scanned white gutter is not.
    #
    # The first attempt thresholded at percentile 40, which is wrong here: on a
    # mostly-white page over 40% of the pixels ARE the white background, so the
    # 40th percentile is ~255 and `dark` matched every pixel -- max row ink came
    # out equal to the full page width and no gutter was ever empty.  The
    # threshold has to come from the DARK tail of the distribution, so the
    # percentiles are printed and the cut is derived from them.
    pcts = np.percentile(gray, [0.5, 1, 5, 25, 50, 75, 95])
    print("  gray percentiles 0.5/1/5/25/50/75/95: "
          + " ".join(f"{p:.0f}" for p in pcts))
    # Otsu adapts to the scan's contrast instead of assuming one; take the
    # stricter of Otsu and a fixed light-value cut so a low-contrast scan with a
    # grey background still separates ink from gutter.
    otsu_value, _ = cv2.threshold(gray, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink_limit = int(min(otsu_value, 200))
    print(f"  otsu = {otsu_value:.0f}  ->  ink_limit = {ink_limit}")
    dark = gray <= ink_limit
    row_ink = dark.sum(axis=1)
    col_ink = dark.sum(axis=0)
    print(f"  ink coverage: rows {row_ink.min()}..{row_ink.max()} of {pw}, "
          f"cols {col_ink.min()}..{col_ink.max()} of {ph}")

    # --- rows: gutters between the 10 card rows ----------------------------
    row_gap_limit = max(2, int(row_ink.max() * 0.01))
    row_runs = zero_runs(row_ink, row_gap_limit)
    tall = [(a, b) for a, b in row_runs if b - a >= ph * 0.002]
    print(f"\n  ROW gutters (ink <= {row_gap_limit} of max {row_ink.max()}):")
    print(f"    {len(row_runs)} near-empty runs, {len(tall)} at least 0.2% of page")
    for a, b in tall:
        print(f"      rows {a:>5}-{b:<5} ({b - a + 1:>4}px)  "
              f"= {a / ph * 100:5.2f}% .. {(b + 1) / ph * 100:5.2f}% of page")

    # --- columns: the gutters between the 3 card columns -------------------
    col_gap_limit = max(2, int(col_ink.max() * 0.01))
    col_runs = zero_runs(col_ink, col_gap_limit)
    tall_cols = [(a, b) for a, b in col_runs if b - a >= pw * 0.001]
    print(f"\n  COLUMN gutters (ink <= {col_gap_limit} of max {col_ink.max()}):")
    print(f"    {len(col_runs)} near-empty runs, {len(tall_cols)} at least 0.1% of page")
    for a, b in tall_cols:
        print(f"      cols {a:>5}-{b:<5} ({b - a + 1:>4}px)  "
              f"= {a / pw * 100:5.2f}% .. {(b + 1) / pw * 100:5.2f}% of width")

    # --- what the constants say, for comparison ---------------------------
    rects = A._voter_card_rects(page)
    print(f"\n  the constants currently produce {len(rects)} positions")
    tops = sorted({round(r.y0 / H, 4) for r in rects})
    lefts = sorted({round(r.x0 / W, 4) for r in rects})
    print(f"    distinct row tops ({len(tops)}): "
          f"{[f'{t * 100:.2f}%' for t in tops[:4]]} ... "
          f"{[f'{t * 100:.2f}%' for t in tops[-2:]]}")
    print(f"    distinct column lefts ({len(lefts)}): "
          f"{[f'{t * 100:.2f}%' for t in lefts]}")
    doc.close()
    print()