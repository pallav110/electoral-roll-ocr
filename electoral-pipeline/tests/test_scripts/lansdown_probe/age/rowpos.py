"""Where is the printed age row, exactly, on each roll?

Guessing the fraction has already failed once.  This measures it: the card is
divided into ink bands, each band is read, and the band carrying the आयु label is
reported as a fraction of card height.  That number is the only thing worth
putting in AGE_REGION.
"""
import sys

sys.stdout = open("/data/rowpos.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

SCALE = 200 / 72.0
CASES = (("Lansdown", "/data/roll107.pdf", 3, 4),
         ("S24-53", "/data/roll_s24_300.pdf", 3, 4))

for label, path, page_no, ncards in CASES:
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]

    print("=" * 76)
    print(f"{label}   card rows for {ncards} cards")
    print("=" * 76)

    for idx in range(ncards):
        r = rects[idx]
        card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
                   int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
        h, w = card.shape[:2]
        gray = cv2.cvtColor(card, cv2.COLOR_RGB2GRAY)

        # Bands: rows carrying ink, grouped with a gap scaled to the card.
        dark = (gray < 140).sum(axis=1)
        rows = np.where(dark > 2)[0]
        gap = max(3, h // 120)
        bands, cur = [], []
        for rr in rows:
            rr = int(rr)
            if cur and rr - cur[-1] <= gap:
                cur.append(rr)
            else:
                if cur:
                    bands.append(cur)
                cur = [rr]
        if cur:
            bands.append(cur)

        found = []
        for b in bands:
            y0, y1 = b[0], b[-1]
            if y1 - y0 < 4:
                continue
            seg = gray[y0:y1 + 1, :]
            cols = np.where((seg < 140).sum(axis=0) > 0)[0]
            if not len(cols):
                continue
            # Read only the left part, where the label sits.
            crop = seg[:, cols[0]:cols[0] + int(w * 0.45)]
            up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            txt = " ".join(pytesseract.image_to_string(
                Image.fromarray(up), lang="hin+eng",
                config="--oem 3 --psm 7").split())
            if "आयु" in txt or "आय" in txt or "आग" in txt:
                found.append((y0 / h, (y1 + 1) / h, txt[:34]))

        if found:
            lo = min(f[0] for f in found)
            hi = max(f[1] for f in found)
            print(f"  card {idx} (h={h}): age row at {lo:.3f}..{hi:.3f}")
            for f in found:
                print(f"        {f[0]:.3f}..{f[1]:.3f}  {f[2]!r}")
        else:
            # No band matched the label; list every band so the row is findable.
            print(f"  card {idx} (h={h}): NO band matched the age label; bands:")
            for b in bands:
                y0, y1 = b[0], b[-1]
                seg = gray[y0:y1 + 1, :]
                cols = np.where((seg < 140).sum(axis=0) > 0)[0]
                if not len(cols):
                    continue
                crop = seg[:, cols[0]:cols[0] + int(w * 0.45)]
                up = cv2.resize(crop, None, fx=2, fy=2,
                                interpolation=cv2.INTER_CUBIC)
                txt = " ".join(pytesseract.image_to_string(
                    Image.fromarray(up), lang="hin+eng",
                    config="--oem 3 --psm 7").split())
                print(f"        {y0 / h:.3f}..{(y1 + 1) / h:.3f}  {txt[:40]!r}")
    doc.close()
    print()