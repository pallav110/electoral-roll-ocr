import sys, re
sys.stdout = open("/data/ac_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

# Patterns to try. Each must be anchored on a printed AC label so a year,
# booth number or voter total cannot become an AC code.
PATTERNS = {
    "current": r"विधानसभा\s*निर्वाचन\s*क्षेत्र[^:\n;：]*[:;：]\s*(\d+)\s*[-–—]",
    "loose_label": r"विधानसभा[^:\n;：]{0,30}क्षेत्र[^:\n;：]{0,20}[:;：]\s*(\d+)\s*[-–—]",
    "any_label": r"(?:विधानसभा|सांसदीय)[^:\n;：]{0,40}[:;：]\s*(\d+)\s*[-–—]",
}

CASES = (("S28-40 Lansdown", "/data/roll107.pdf", "40"),
         ("S24-53 works", "/data/roll_s24_300.pdf", "53"))

for label, path, truth in CASES:
    d = fitz.open(path)
    print("=" * 76); print(f"{label}   truth AC = {truth}"); print("=" * 76)
    page = d[2]
    H = page.rect.height
    for pct, page_ix in ((0.06, 2), (0.06, 0), (0.10, 2)):
        h = max(40.0, H * pct)
        clip = A.fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + h)
        img = np.frombuffer(page.get_pixmap(dpi=300, clip=clip, alpha=False).tobytes("png"), np.uint8)
        im = cv2.imdecode(img, cv2.IMREAD_COLOR)
        if im is None: continue
        for psm in (6, 4):
            t = pytesseract.image_to_string(Image.fromarray(im), lang="hin+eng",
                                            config=f"--oem 3 --psm {psm}")
            flat = " ".join(t.translate(str.maketrans("०१२३४५६७८९", "0123456789")).split())
            print(f"\n  page{page_ix+1} {pct:.0%} psm{psm}: {flat[:150]!r}")
            for pn, pat in PATTERNS.items():
                m = re.search(pat, flat)
                got = m.group(1) if m else None
                mark = "OK " if got == truth else "   "
                print(f"     {mark}{pn:11} -> {got}")
    print()