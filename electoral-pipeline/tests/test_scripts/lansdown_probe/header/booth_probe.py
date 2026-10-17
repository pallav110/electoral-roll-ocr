import sys, re
sys.stdout = open("/data/booth_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

d = fitz.open("/data/roll107.pdf"); page = d[0]
H = page.rect.height
print(f"page height {H:.0f}pt")

for pct in (0.09, 0.12, 0.15, 0.20):
    h = max(130.0, H * pct)
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + h)
    img = A.np.frombuffer(page.get_pixmap(dpi=300, clip=clip, alpha=False).tobytes("png"), A.np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    text = pytesseract.image_to_string(Image.fromarray(im), lang="hin+eng", config="--oem 3 --psm 6")
    flat = " ".join(text.split())
    m = re.search(r"(?:भाग\s*संख्या|part\s*(?:number|no\.?))(?:\s*[:：;,.])*\s*(\d+)", flat,
                  flags=re.IGNORECASE)
    print(f"\nclip {h:.0f}pt ({h/H*100:.1f}%): match={m.group(1) if m else None}")
    print(f"  text[:260]: {flat[:260]!r}")
    if not m:
        for probe in ("भाग", "संख्या", "107", "पिन", "कोड", "40"):
            print(f"    contains {probe!r}: {probe in flat}")