import sys, io, os, json
sys.stdout = open("/data/thresh_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
import ocr_pdf_api as A
import pytesseract
from pytesseract import Output

d = fitz.open("/data/roll107.pdf")
page = d[2]
SCALE = 200 / 72.0
pix = page.get_pixmap(dpi=200)
raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
r = A._voter_card_rects(page)[0]
y0, y1 = int(r.y0*SCALE), int(r.y1*SCALE)
x0, x1 = int(r.x0*SCALE), int(r.x1*SCALE)
crop = raw[y0:y1, x0:x1].copy()
cv2.imwrite("/data/t0_raw.png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))

def words(img, tag, lang="hin+eng"):
    data = pytesseract.image_to_data(img, lang=lang, config="--psm 6", output_type=Output.DICT)
    got = [t for t in data["text"] if t.strip()]
    confs = [float(data["conf"][i]) for i, t in enumerate(data["text"]) if t.strip()]
    mean = sum(confs)/len(confs) if confs else 0
    print(f"\n--- {tag}: {len(got)} words, mean conf {mean:.1f}")
    print("   ", got[:14])
    return mean

print("=== A: raw crop, no preprocessing, psm 6 ===")
words(crop, "raw psm6")

print("\n=== B: lang variations on the RAW crop ===")
for lang in ("hin", "eng", "hin+eng"):
    words(crop, f"raw lang={lang}", lang)

print("\n=== C: reproduce the pipeline's exact preprocessing ===")
up = cv2.resize(crop, (0, 0), fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
cv2.imwrite("/data/t1_thresholded.png", th)
cv2.imwrite("/data/t2_upscaled.png", cv2.cvtColor(up, cv2.COLOR_RGB2BGR))
words(th, "thresholded (pipeline path)")
words(cv2.cvtColor(up, cv2.COLOR_RGB2BGR), "upscaled only, no threshold")

print("\n=== D: invert test (is the polarity wrong?) ===")
words(255 - th, "inverted thresholded")

print("\n=== E: crop bounds sanity ===")
print("rect:", r, "scale:", SCALE)
print("pixmap:", pix.width, "x", pix.height, "n channels:", pix.n)
print("page.rect:", page.rect)
print("crop y:", y0, y1, " x:", x0, x1)
print("pixel at (y0+10, x0+10):", crop[10, 10], " pixel at (y0+300, x0+900):", crop[300, 900])