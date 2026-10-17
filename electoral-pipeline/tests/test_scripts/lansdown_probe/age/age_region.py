"""AGE_REGION is positioned by a fraction calibrated on S24-53.

On the Lansdown card the region starts at 0.59 of card height while the printed
आयु row begins at ~0.55, so the crop lands in blank white below the digits.  The
reader then finds noise and returns 88.

This measures the real position of the printed age row on BOTH rolls, by finding
the ink bands and locating the one containing the आयु label, so the fix is
anchored on where the text is rather than on another constant.

It also reports what each reader returns for each band, to confirm 88 is noise
picked up from empty space and not from a neighbouring card.
"""
import sys

sys.stdout = open("/data/age_region.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
CASES = (("Lansdown", "/data/roll107.pdf"), ("S24-53", "/data/roll_s24_300.pdf"))


def bands(gray, gap):
    dark = (gray < 140).sum(axis=1)
    rows = np.where(dark > 2)[0]
    if not len(rows):
        return []
    out, cur = [], [int(rows[0])]
    for r in rows[1:]:
        r = int(r)
        if r - cur[-1] <= gap:
            cur.append(r)
        else:
            out.append(cur)
            cur = [r]
    out.append(cur)
    return [(g[0], g[-1]) for g in out]


for label, path in CASES:
    doc = fitz.open(path)
    page = doc[3]
    rects = A._voter_card_rects(page)
    r = rects[0]
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    h, w = card.shape[:2]
    gray = cv2.cvtColor(card, cv2.COLOR_RGB2GRAY)
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card

    print("=" * 76)
    print(f"{label}   card {w}x{h}")
    print("=" * 76)
    print(f"  AGE_REGION        rows {int(h*A.AGE_REGION[1])}..{int(h*A.AGE_REGION[3])}"
          f"  = {A.AGE_REGION[1]:.2f}..{A.AGE_REGION[3]:.2f} of height")
    print(f"  GENDER_AGE_REGION rows {int(h*A.GENDER_AGE_REGION[1])}.."
          f"{int(h*A.GENDER_AGE_REGION[3])}  = "
          f"{A.GENDER_AGE_REGION[1]:.2f}..{A.GENDER_AGE_REGION[3]:.2f}")

    print("\n  ink bands on the card, as fractions of height:")
    age_band = None
    for y0, y1 in bands(gray, max(4, h // 90)):
        seg = gray[y0:y1 + 1, :]
        cols = np.where((seg < 140).sum(axis=0) > 0)[0]
        if not len(cols):
            continue
        crop = seg[:, cols[0]:cols[0] + int(w * 0.55)]
        up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        from PIL import Image
        import pytesseract
        txt = " ".join(pytesseract.image_to_string(
            Image.fromarray(up), lang="hin+eng",
            config="--oem 3 --psm 7").split())
        p0, p1 = y0 / h, (y1 + 1) / h
        mark = ""
        if "आयु" in txt or "आय" in txt:
            mark = "   <== AGE ROW"
            age_band = (p0, p1)
        print(f"    {p0:5.3f}..{p1:5.3f}  {txt[:58]!r}{mark}")

    if age_band:
        print(f"\n  the printed age row spans {age_band[0]:.3f}..{age_band[1]:.3f}")
        print(f"  AGE_REGION starts at   {A.AGE_REGION[1]:.3f}  -> "
              f"{'MISSES the row' if A.AGE_REGION[1] > age_band[1] else 'overlaps' if A.AGE_REGION[1] < age_band[0] else 'inside'}")
        print(f"  a region covering it would start at about "
              f"{max(0.0, age_band[0] - 0.04):.3f}")

    print(f"\n  _extract_age_from_clean_card -> "
          f"{A._extract_age_from_clean_card(clean)!r}")
    print(f"  _extract_card age            -> "
          f"{(A._extract_card(page, r, 4, 0) or {}).get('age')!r}")
    doc.close()
    print()