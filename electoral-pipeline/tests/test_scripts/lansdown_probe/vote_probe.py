import sys, os, re, json
sys.stdout = open("/data/vote_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
from PIL import Image
import ocr_pdf_api as A
from pytesseract import Output

FILES = [("S24-300 WORKS", "/data/roll_s24_300.pdf"),
         ("S28-40 Lansdown BROKEN", "/data/roll107.pdf")]

KEY_FIELDS = ("voter_first_name", "voter_father_name", "voter_husband_name",
              "voter_mother_name", "voter_other_name", "voter_sur_name")

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

def score(lines):   return A._hindi_line_score(lines)
def conf_key(lines, c): return c
def labbonly(lines):
    text = " ".join(lines)
    return sum(w for lab, w in A.HINDI_FIELD_LABELS if lab in text)

STRATEGIES = {
    "CURRENT (score, conf)": lambda ls, c: (score(ls), c),
    "PROPOSED (labels, conf)": lambda ls, c: (labbonly(ls), c),
    "ALT (conf, score)": lambda ls, c: (c, score(ls)),
}

for label, path in FILES:
    print("=" * 78); print(label); print("=" * 78)
    d = fitz.open(path); SCALE = 200 / 72.0
    tally = {k: {"names": 0, "cards": 0} for k in STRATEGIES}
    per_strategy_fields = {k: [] for k in STRATEGIES}
    picks = {k: 0 for k in STRATEGIES}
    N = 30
    for pg in (2, 3):
        page = d[pg]
        pix = page.get_pixmap(dpi=200)
        raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        for r in A._voter_card_rects(page):
            crop = raw[int(r.y0*SCALE):int(r.y1*SCALE), int(r.x0*SCALE):int(r.x1*SCALE)].copy()
            up = cv2.resize(crop, (0, 0), fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
            thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                        cv2.THRESH_BINARY, 11, 2)
            cands = {}
            for nm, v in (("gray", gray), ("thresh", thr)):
                ls, c = lines_of(v)
                if ls: cands[nm] = (ls, c)
            if not cands: continue
            for sname, keyfn in STRATEGIES.items():
                win = max(cands.items(), key=lambda kv: keyfn(*kv[1]))[0]
                picks[sname] += (1 if win == "thresh" else 0)
                rec = A.parse_voter_box_from_ocr_lines(cands[win][0])
                nf = sum(1 for k in KEY_FIELDS if rec.get(k))
                tally[sname]["names"] += nf
                tally[sname]["cards"] += 1
                per_strategy_fields[sname].append(nf)
    total = tally["CURRENT (score, conf)"]["cards"]
    print(f"\ncards evaluated: {total}")
    print(f"{'strategy':>26} {'name fields filled':>20} {'/cards*2':>10} {'%':>7} {'picked thresh':>14}")
    for sname in STRATEGIES:
        t = tally[sname]
        f = t["names"]
        pct = 100.0 * f / (t["cards"] * 2) if t["cards"] else 0
        print(f"{sname:>26} {f:>20} {t['cards']:>10} {pct:>6.1f}% {picks[sname]:>14}")
    print()