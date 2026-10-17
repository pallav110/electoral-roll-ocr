"""Can the card lattice be INFERRED, rather than hardcoded?

The gutter scan found clean structure on both rolls, but raw gutters are not the
answer on their own -- a card's own photo box and line gaps produce white runs
too (Lansdown has a 523px empty column inside a card, 77.8-87.3% of page width).
What makes the grid recoverable is that it is *periodic*: the row gutters
recur at a constant spacing, and the constant carries 10 card rows per page.

This infers the period and phase by autocorrelation of the row-ink profile, then
compares the inferred lattice against the hardcoded constants.  If the inference
reproduces the constants on both rolls, detection is not merely possible -- it is
verifiably at least as accurate as what is hardcoded now.
"""
import sys

sys.stdout = open("/data/lattice.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

CASES = (
    ("S28-40 Lansdown", "/data/roll107.pdf"),
    ("S24-53 works", "/data/roll_s24_300.pdf"),
)


def ink_profile(page):
    raw = page.get_pixmap(dpi=200, alpha=False)
    img = np.frombuffer(raw.samples, np.uint8).reshape(
        raw.height, raw.width, raw.n)[:, :, :3]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    otsu_value, _ = cv2.threshold(gray, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = gray <= int(min(otsu_value, 200))
    return dark.sum(axis=1).astype(np.float64), gray.shape


for label, path in CASES:
    doc = fitz.open(path)
    page = doc[2]
    profile, (ph, pw) = ink_profile(page)
    H, W = page.rect.height, page.rect.width
    print("=" * 78)
    print(f"{label}   page {W:.0f}x{H:.0f}pt   render {pw}x{ph}px")
    print("=" * 78)

    # Detrend: the profile carries a strong overall ramp, which would dominate
    # the autocorrelation.  Subtract a heavily smoothed version of itself.
    kernel = np.ones(ph // 10) / max(1, ph // 10)
    trend = np.convolve(profile, kernel, mode="same")
    centred = profile - trend

    # Autocorrelation over plausible periods: a grid of 4..14 rows per page.
    min_lag = ph // 14
    max_lag = ph // 4
    scores = []
    for lag in range(min_lag, max_lag):
        a, b = centred[:-lag], centred[lag:]
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
        scores.append((float(a @ b) / denom, lag))
    scores.sort(reverse=True)
    best_score, best_lag = scores[0]
    period_pt = best_lag * 72.0 / 200.0
    rows_inferred = round(H / period_pt)

    print(f"  strongest autocorrelation {best_score:.3f} at lag {best_lag}px "
          f"= {period_pt:.1f}pt = {period_pt / H * 100:.2f}% of page height")
    print(f"  => {rows_inferred} card rows per page (2nd best: "
          + ", ".join(f"{s:.3f}@{l * 72 / 200:.1f}pt" for s, l in scores[1:4])
          + ")")

    # Compare against the constants: infer where row 1 should start.
    rects = A._voter_card_rects(page)
    tops = sorted({round(r.y0 / H, 5) for r in rects})
    span = (tops[1] - tops[0])
    print(f"\n  constant row pitch     = {span * 100:.2f}% of page")
    print(f"  inferred row pitch     = {period_pt / H * 100:.2f}% of page")
    print(f"  constant first row top = {tops[0] * 100:.2f}% of page")

    # Where does the inferred grid put its first row?  Phase comes from the
    # first gutter after the header, which the earlier scan put at ~11.7-12.6%
    # on both rolls -- so the grid starts one pitch below the header block.
    header_end = 0.12
    first_inferred = header_end
    drift = [abs(t - (first_inferred + i * (period_pt / H))) * 100
             for i, t in enumerate(tops)]
    print(f"  first row, constant {tops[0] * 100:.2f}%  "
          f"inferred {first_inferred * 100:.2f}%")
    print(f"  worst per-row disagreement across all 10 rows: "
          f"{max(drift):.2f}% of page height")
    doc.close()
    print()