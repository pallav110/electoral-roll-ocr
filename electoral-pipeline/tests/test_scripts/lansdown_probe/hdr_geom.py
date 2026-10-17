import sys, json
sys.stdout = open("/data/geom_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2, re
from PIL import Image
import pytesseract
import ocr_pdf_api as A

# Where does the header text ACTUALLY sit on page 3, as a fraction of page height?
for label, path in (("S28-40 Lansdown", "/data/roll107.pdf"),
                    ("S24-53 works", "/data/roll_s24_300.pdf")):
    d = fitz.open(path); page = d[2]
    H = page.rect.height
    band = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + H * 0.16)
    img = np.frombuffer(page.get_pixmap(dpi=200, clip=band, alpha=False).tobytes("png"), np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    dark = (g < 140).sum(axis=1)
    rows = np.where(dark > 2)[0]
    groups = []
    if len(rows):
        cur = [rows[0]]
        for r in rows[1:]:
            if r - cur[-1] <= 8: cur.append(r)
            else: groups.append(cur); cur = [r]
        groups.append(cur)
    print("=" * 74)
    print(f"{label}  page {page.rect.width:.0f}x{H:.0f}pt   band = top 16% = {h}px")
    print("  text rows as % of PAGE height:")
    for gp in groups:
        y0, y1 = gp[0] / h * 16, gp[-1] / h * 16
        if y1 - y0 < 0.08: continue
        seg = g[gp[0]:gp[-1]+1, :]
        cols = np.where((seg < 140).sum(axis=0) > 0)[0]
        crop = seg[:, cols[0]:cols[-1]+1] if len(cols) else seg
        up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        t = " ".join(pytesseract.image_to_string(Image.fromarray(up), lang="hin+eng",
                                                config="--oem 3 --psm 7").split())
        print(f"    {y0:5.2f}% - {y1:5.2f}%  | {t[:95]!r}")

    # what the CURRENT 10% clip implies for the anubhag name crop fractions
    print(f"\n  with header_height = max(52, {H*0.10:.0f}pt):")
    clip_h = max(52.0, H * 0.10)
    for nm, y0, y1 in (("ac", 0.06, 0.39), ("booth", 0.06, 0.39),
                       ("anubhag_name", 0.33, 0.68), ("anubhag_code", 0.33, 0.68)):
        print(f"    {nm:13} crop y = {y0*clip_h/H*100:5.2f}% .. {y1*clip_h/H*100:5.2f}% of page")
    print(f"    (what S24's old 52pt gave: {0.06*52/842*100:.2f}% .. {0.39*52/842*100:.2f}%"
          f" and name {0.33*52/842*100:.2f}% .. {0.68*52/842*100:.2f}%)")
    print()