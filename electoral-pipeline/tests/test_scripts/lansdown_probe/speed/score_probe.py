import sys, os
sys.stdout = open("/data/score_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2, re
from PIL import Image
import ocr_pdf_api as A
from pytesseract import Output

FILES = [("S24-300 (WORKS)", "/data/roll_s24_300.pdf"),
         ("S28-40 Lansdown (BROKEN)", "/data/roll107.pdf")]

def lines_of(variant):
    data = A._tess_data(Image.fromarray(variant), lang=A.TESSERACT_HINDI_LANG,
                        config=A.TESSERACT_CONFIG)
    lines, confs, cur, curc, last = [], [], [], [], None
    for i, text in enumerate(data["text"]):
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if text.strip():
            if key != last and cur:
                lines.append(" ".join(cur))
                if curc: confs.append(sum(curc)/len(curc))
                cur, curc = [], []
            cur.append(text.strip())
            if data["conf"][i] >= 0: curc.append(data["conf"][i])
            last = key
    if cur:
        lines.append(" ".join(cur))
        if curc: confs.append(sum(curc)/len(curc))
    return lines, (sum(confs)/len(confs) if confs else 0.0)

for label, path in FILES:
    print("=" * 78); print(label); print("=" * 78)
    d = fitz.open(path); page = d[2]
    pix = page.get_pixmap(dpi=200)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    SCALE = 200 / 72.0
    rects = A._voter_card_rects(page)

    for idx in (0, 1, 11):
        r = rects[idx]
        crop = raw[int(r.y0*SCALE):int(r.y1*SCALE), int(r.x0*SCALE):int(r.x1*SCALE)].copy()
        up = cv2.resize(crop, (0, 0), fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 11, 2)
        print(f"\n--- card {idx} ---")
        results = {}
        for name, variant in (("gray(raw)", gray), ("thresh", thresh)):
            lines, conf = lines_of(variant)
            if not lines:
                print(f"  {name:10} -> NO LINES")
                continue
            sc = A._hindi_line_score(lines)
            text = " ".join(lines)
            nchars = len(re.findall(r"[ऀ-ॿ]", text))
            labels_found = [lab for lab, _ in A.HINDI_FIELD_LABELS if lab in text]
            results[name] = (sc, conf)
            print(f"  {name:10} score={sc:>5} conf={conf:>5.1f} deva_chars={nchars:>4} "
                  f"labels={len(labels_found)}{labels_found}")
            print(f"             lines[:4]: {[l[:58] for l in lines[:4]]}")
        if len(results) == 2:
            winner = max(results.items(), key=lambda kv: (kv[1][0], kv[1][1]))
            print(f"  >>> WINNER (current code): {winner[0]}")
            rec = A.parse_voter_box_from_ocr_lines(
                lines_of(gray if winner[0] == "gray(raw)" else thresh)[0])
            filled = {k: v for k, v in rec.items() if v and k != "empty"}
            print(f"  >>> PARSED {len(filled)} fields: {filled}")
    print()