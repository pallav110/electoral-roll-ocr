import sys, io, os, json
out = open("/data/chain_out.txt", "w", encoding="utf-8", errors="replace")
sys.stdout = out
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
import ocr_pdf_api as A
import pytesseract
from pytesseract import Output

PDF = "/data/roll107.pdf"
d = fitz.open(PDF)
page = d[2]
SCALE = 200 / 72.0
pix = page.get_pixmap(dpi=200)
raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
rgb = raw[:, :, :3].copy()
bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

r = A._voter_card_rects(page)[0]
y0, y1 = int(r.y0*SCALE), int(r.y1*SCALE)
x0, x1 = int(r.x0*SCALE), int(r.x1*SCALE)

def read(img, tag):
    ok, buf = cv2.imencode(".png", img)
    lines, conf = A._extract_text_with_tesseract(buf.tobytes())
    print(f"\n--- {tag}: shape={img.shape} conf={conf}")
    for ln in (lines or [])[:6]:
        print("    |", ln)
    return lines

crop_rgb = rgb[y0:y1, x0:x1]
crop_bgr = bgr[y0:y1, x0:x1]
cv2.imwrite("/data/step0_crop.png", crop_bgr)

lines_rgb = read(crop_rgb, "RAW crop as RGB (my local path)")
lines_bgr = read(crop_bgr, "RAW crop as BGR")

# now the pipeline preprocessing chain, on BGR as the code expects
masked = A._mask_photo_box(crop_bgr, A.PHOTO_BOX_REGION)
cv2.imwrite("/data/step1_photo_masked.png", masked)
lines1 = read(masked, "after _mask_photo_box")

cleaned = A._remove_outer_card_lines(masked)
cv2.imwrite("/data/step2_lines_removed.png", cleaned)
lines2 = read(cleaned, "after _remove_outer_card_lines")

print("\n\n########## SUMMARY ##########")
print("RGB crop conf :", A._extract_text_with_tesseract(cv2.imencode('.png', crop_rgb)[1].tobytes())[1])
print("BGR crop conf :", A._extract_text_with_tesseract(cv2.imencode('.png', crop_bgr)[1].tobytes())[1])
print("masked  conf  :", A._extract_text_with_tesseract(cv2.imencode('.png', masked)[1].tobytes())[1])
print("cleaned conf  :", A._extract_text_with_tesseract(cv2.imencode('.png', cleaned)[1].tobytes())[1])

# what does the parser say for the best variant?
best = None
for tag, ls in (("rgb", lines_rgb), ("bgr", lines_bgr), ("masked", lines1), ("cleaned", lines2)):
    if ls:
        rec = A.parse_voter_box_from_ocr_lines(ls)
        filled = sum(1 for k, v in rec.items() if v and k != "empty")
        print(f"\nPARSED[{tag}]: {filled} fields ->",
              {k: v for k, v in rec.items() if v and k != "empty"})