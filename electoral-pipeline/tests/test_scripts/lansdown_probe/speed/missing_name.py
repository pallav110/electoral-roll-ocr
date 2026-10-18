"""Find the one card per page that still misses names on Lansdown HIN-107.

We fixed names from 2/30 to 29/30.  This finds the one still missing,
renders it, and reads it — so we know whether it's genuinely unreadable
or a fixable edge case.
"""
import sys
sys.stdout = open("/data/missing_name.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]   # page 4
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]

missing = []
for i, r in enumerate(rects):
    rec = A._extract_card(page, r, 4, i) or {}
    name = (rec.get("voter_first_name") or "").strip()
    if not name:
        missing.append((i, rec))
        card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
                   int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
        cv2.imwrite(f"/data/miss_card{i}.png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))

print(f"Cards with no first name: {[m[0] for m in missing]}")
for idx, rec in missing:
    print(f"\n  card {idx}:")
    print(f"    epic        = {rec.get('id_card_no')!r}")
    print(f"    house_no    = {rec.get('house_no')!r}")
    print(f"    age         = {rec.get('age')!r}")
    print(f"    gender      = {rec.get('gender')!r}")
    print(f"    first_name  = {rec.get('voter_first_name')!r}")
    print(f"    sur_name    = {rec.get('voter_sur_name')!r}")
    print(f"    relation    = {rec.get('relation_name')!r}")
    print(f"    father_name = {rec.get('voter_father_name')!r}")
    print(f"    is_deleted  = {rec.get('is_deleted')!r}")
    print(f"    needs_review= {rec.get('needs_review')!r}")
