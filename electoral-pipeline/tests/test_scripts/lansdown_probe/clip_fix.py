import sys, json
sys.stdout = open("/data/clipfix_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
from PIL import Image
import ocr_pdf_api as A

for label, path in (("S28-40 Lansdown", "/data/roll107.pdf"),
                    ("S24-53 works", "/data/roll_s24_300.pdf")):
    print("=" * 72); print(label); print("=" * 72)
    d = fitz.open(path); page = d[0]
    clip = A._cover_header_clip(page)
    print(f"clip height {clip.height:.0f}pt = {clip.height/page.rect.height*100:.2f}% of page")
    print("_read_state_token ->", repr(A._read_state_token(page)))
    print("_extract_state_code ->", A._extract_state_code(page))
    print("_extract_booth_code_from_cover ->", repr(A._extract_booth_code_from_cover(page)))

    # where does the x-window 0.48-0.60 actually land relative to the token?
    img = A.np.frombuffer(page.get_pixmap(dpi=A._STATE_TOKEN_DPI, clip=clip, alpha=False)
                          .tobytes("png"), A.np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    h, w = im.shape[:2]
    x0, x1 = int(w * 0.48), int(w * 0.60)
    y0, y1 = int(h * 0.50), int(h * 0.92)
    print(f"\nrendered clip: {w}x{h}; x-window {x0}-{x1} ({x0/w*100:.0f}%-{x1/w*100:.0f}%), "
          f"y-window {y0}-{y1} ({y0/h*100:.0f}%-{y1/h*100:.0f}%)")
    sub = cv2.cvtColor(im[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    print("ink in that window:", int((sub < 140).sum()), "px")
    cv2.imwrite(f"/data/win_{label.split()[0]}.png", sub)
    print("OCR of window:", repr(A._tess_string(Image.fromarray(sub), lang="eng", config="--oem 3 --psm 7"))[:120])
    print()