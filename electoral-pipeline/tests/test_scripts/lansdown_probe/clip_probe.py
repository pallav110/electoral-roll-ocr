import sys, json
sys.stdout = open("/data/clip_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
from PIL import Image
import ocr_pdf_api as A

for label, path in (("S28-40 Lansdown", "/data/roll107.pdf"),
                    ("S24-53 (works)", "/data/roll_s24_300.pdf")):
    print("=" * 72); print(label); print("=" * 72)
    d = fitz.open(path)
    page = d[0]
    H, W = page.rect.height, page.rect.width
    print(f"page: {W:.0f} x {H:.0f} pt")
    clip = A._cover_header_clip(page)
    print(f"_cover_header_clip -> height {clip.height:.0f}pt = {clip.height/H*100:.2f}% of page")

    # render the top 15% of the page and find the ink bands
    band = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + H * 0.15)
    img = A.np.frombuffer(page.get_pixmap(dpi=200, clip=band, alpha=False).tobytes("png"),
                          A.np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    print(f"top-15% band rendered: {w} x {h} px  (covers 0 - 15% of page)")
    dark = (g < 140).sum(axis=1)
    rows = np.where(dark > 3)[0]
    groups = []
    if len(rows):
        cur = [rows[0]]
        for r in rows[1:]:
            if r - cur[-1] <= 6: cur.append(r)
            else: groups.append(cur); cur = [r]
        groups.append(cur)
    print("\nink rows in top 15% (page-% of page, y-frac within band, x-span):")
    for gp in groups:
        y0, y1 = gp[0], gp[-1]
        pg_y0 = (y0 / h) * 0.15 * 100
        pg_y1 = (y1 / h) * 0.15 * 100
        if pg_y1 - pg_y0 < 0.05: continue
        seg = g[y0:y1+1, :]
        cols = np.where((seg < 140).sum(axis=0) > 1)[0]
        if not len(cols): continue
        x0f, x1f = cols[0]/w*100, cols[-1]/w*100
        # OCR this band
        crop = g[y0:y1+1, cols[0]:cols[-1]+1]
        up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        txt = A._tess_string(Image.fromarray(up), lang="hin+eng", config="--psm 7")
        print(f"  {pg_y0:5.2f}% - {pg_y1:5.2f}%  x {x0f:5.1f}%-{x1f:5.1f}%  | {txt[:70]!r}")
    print(f"\nCLIP covers 0 - {clip.height/H*100:.2f}%  => bands above that line are INVISIBLE to state reader")
    print()