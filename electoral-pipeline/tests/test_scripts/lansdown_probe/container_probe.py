import sys, io, os, json
sys.stdout = open("/data/probe_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
import ocr_pdf_api as A

PDF = sys.argv[1]
print("=== BACKEND INFO:", A._tess_backend_info())
print("=== CARD WORKERS:", A._OCR_CARD_WORKERS)
import tesserocr
print("=== tesserocr version:", tesserocr.tesseract_version())

d = fitz.open(PDF)
page = d[2]
SCALE = 200 / 72.0
pix = page.get_pixmap(dpi=200)
img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]

rects = A._voter_card_rects(page)
print("=== rects:", len(rects))

r = rects[0]
crop = img[int(r.y0*SCALE):int(r.y1*SCALE), int(r.x0*SCALE):int(r.x1*SCALE)]
print("=== crop:", crop.shape)

# ---- replicate the pipeline's own read, verbatim ----
ok, buf = cv2.imencode(".png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
lines, conf = A._extract_text_with_tesseract(buf.tobytes())
print("\n=== _extract_text_with_tesseract lines (the real path) ===")
print("   conf:", conf)
for ln in (lines or []):
    print("   |", ln)

if lines:
    rec = A.parse_voter_box_from_ocr_lines(lines)
    print("\n=== PARSED RECORD ===")
    print(json.dumps({k: v for k, v in rec.items() if v}, ensure_ascii=False, indent=2))
else:
    print("\n!!! NO LINES RETURNED BY THE REAL PATH")