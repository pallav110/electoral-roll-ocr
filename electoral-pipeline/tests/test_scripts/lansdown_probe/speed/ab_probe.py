import sys, os
sys.stdout = open("/data/ab_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
import ocr_pdf_api as A
import pytesseract
from pytesseract import Output

FILES = [("S24-300 (WORKS on UAT)", "/data/roll_s24_300.pdf"),
         ("S28-40 Lansdown (BROKEN)", "/data/roll107.pdf")]

def stats(img):
    data = pytesseract.image_to_data(img, lang="hin+eng", config="--psm 6", output_type=Output.DICT)
    words = [t for t in data["text"] if t.strip()]
    confs = [float(data["conf"][i]) for i, t in enumerate(data["text"]) if t.strip()]
    mean = sum(confs) / len(confs) if confs else 0.0
    deva = sum(1 for w in words if any('ऀ' <= c <= 'ॿ' for c in w))
    return len(words), mean, deva, words

def threshold_variant(crop):
    up = cv2.resize(crop, (0, 0), fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 11, 2)

for label, path in FILES:
    print("=" * 78)
    print(label)
    print("=" * 78)
    d = fitz.open(path)
    print(f"pages: {d.page_count}")

    # image compression probe on a voter page
    page = d[2]
    imgs = page.get_images(full=True)
    for i in imgs[:2]:
        print(f"  embedded image: {i[2]}x{i[3]} px, filter={i[4]}, bpc={i[5]}, enc={i[8]}")
    # JPEG compression artefacts: count mid-tone pixels (noise floor)
    pix = page.get_pixmap(dpi=200)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    g = cv2.cvtColor(raw, cv2.COLOR_RGB2GRAY)
    near_white = ((g > 200) & (g < 250)).mean()
    pure_white = (g >= 250).mean()
    print(f"  paper pixels: pure-white {pure_white:.3f}, near-white(noise) {near_white:.3f}")
    print(f"  -> {'NOISY (JPEG artifacts)' if near_white > 0.02 else 'CLEAN paper'}")

    rects = A._voter_card_rects(page)
    SCALE = 200 / 72.0
    print(f"\n  {'card':>5} {'RAW words':>10} {'RAW conf':>9} {'RAW deva':>9} | "
          f"{'THR words':>10} {'THR conf':>9} {'THR deva':>9}")
    tot = {}
    for idx in (0, 1, 2, 10, 11, 20):
        r = rects[idx]
        crop = raw[int(r.y0*SCALE):int(r.y1*SCALE), int(r.x0*SCALE):int(r.x1*SCALE)].copy()
        n0, c0, d0, w0 = stats(crop)
        n1, c1, d1, w1 = stats(threshold_variant(crop))
        print(f"  {idx:>5} {n0:>10} {c0:>9.1f} {d0:>9} | {n1:>10} {c1:>9.1f} {d1:>9}")
        tot.setdefault("raw_conf", []).append(c0)
        tot.setdefault("thr_conf", []).append(c1)
        tot.setdefault("raw_deva", []).append(d0)
        tot.setdefault("thr_deva", []).append(d1)
        if idx == 0:
            print(f"         RAW words: {w0[:12]}")
            print(f"         THR words: {w1[:12]}")
    rc = sum(tot["raw_conf"]) / len(tot["raw_conf"])
    tc = sum(tot["thr_conf"]) / len(tot["thr_conf"])
    rd = sum(tot["raw_deva"]) / len(tot["raw_deva"])
    td = sum(tot["thr_deva"]) / len(tot["thr_deva"])
    print(f"\n  AVERAGE  raw conf {rc:.1f} (deva {rd:.0f}/card)  vs  "
          f"thresh conf {tc:.1f} (deva {td:.0f}/card)")
    print(f"  VERDICT: thresholding {'HURTS' if tc < rc else 'helps'} this roll\n")