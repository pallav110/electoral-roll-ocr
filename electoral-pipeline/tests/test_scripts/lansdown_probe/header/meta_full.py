import sys, json, time
sys.stdout = open("/data/meta_full_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2, re
from PIL import Image
import pytesseract
import ocr_pdf_api as A

for label, path, truth in (
        ("S28-40 Lansdown", "/data/roll107.pdf", {"state": "S28", "ac": "40", "booth": "107"}),
        ("S24-53 works", "/data/roll_s24_300.pdf", {"state": "S24", "ac": "?", "booth": "300"})):
    print("=" * 74); print(label, truth); print("=" * 74)
    d = fitz.open(path); page = d[0]
    H = page.rect.height

    print("_read_state_token           :", repr(A._read_state_token(page)))
    print("_extract_state_code         :", A._extract_state_code(page))
    print("_extract_booth_code_from_cover:", repr(A._extract_booth_code_from_cover(page)))
    print("_extract_anubhag_entries    :", A._extract_anubhag_entries_from_cover(page))

    # full cover OCR: what digits are actually visible?
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + H * 0.20)
    img = np.frombuffer(page.get_pixmap(dpi=300, clip=clip, alpha=False).tobytes("png"), np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    t = " ".join(pytesseract.image_to_string(Image.fromarray(im), lang="hin+eng",
                                            config="--oem 3 --psm 6").split())
    print("\nfull cover text:", t[:300])
    for pat, nm in ((r"भाग\s*संख्या\s*[:：;,.]*\s*(\d+)", "booth"),
                    (r"क्षेत्र\s*की\s*संख्या[^\d]*(\d+)", "ac"),
                    (r"पिन\s*कोड\s*[:：;,.]*\s*(\d+)", "pin")):
        m = re.search(pat, t)
        print(f"  {nm:6} regex -> {m.group(1) if m else None}")

    print("\nfull _extract_roll_header_metadata:")
    t0 = time.time()
    md = A._extract_roll_header_metadata(page)
    print(f"  ({time.time()-t0:.1f}s)")
    print("  ", json.dumps({k: v for k, v in md.items()
                           if k not in ("anubhag_name_candidates", "anubhag_name_raw_reads")},
                          ensure_ascii=False, default=str)[:700])
    print()