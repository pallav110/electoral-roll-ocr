import sys, re
sys.stdout = open("/data/delta_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2
from PIL import Image
import ocr_pdf_api as A

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

def labels_only(ls):
    t = " ".join(ls)
    return sum(w for lab, w in A.HINDI_FIELD_LABELS if lab in t)

d = fitz.open("/data/roll_s24_300.pdf")
SCALE = 200 / 72.0
print("Cards where CURRENT and PROPOSED disagree on S24-300:")
print("-" * 78)
flips = 0
for pg in range(2, 6):
    page = d[pg]
    pix = page.get_pixmap(dpi=200)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    for ci, r in enumerate(A._voter_card_rects(page)):
        crop = raw[int(r.y0*SCALE):int(r.y1*SCALE), int(r.x0*SCALE):int(r.x1*SCALE)].copy()
        up = cv2.resize(crop, (0, 0), fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
        thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 11, 2)
        cands = {}
        for nm, v in (("gray", gray), ("thresh", thr)):
            ls, c = lines_of(v)
            if ls: cands[nm] = (ls, c)
        if len(cands) < 2: continue
        cur = max(cands.items(), key=lambda kv: (A._hindi_line_score(kv[1][0]), kv[1][1]))[0]
        pro = max(cands.items(), key=lambda kv: (labels_only(kv[1][0]), kv[1][1]))[0]
        if cur == pro: continue
        flips += 1
        rc = A.parse_voter_box_from_ocr_lines(cands[cur][0])
        rp = A.parse_voter_box_from_ocr_lines(cands[pro][0])
        fc = {k: rc[k] for k in KEY_FIELDS if rc.get(k)}
        fp = {k: rp[k] for k in KEY_FIELDS if rp.get(k)}
        nc = (A._hindi_line_score(cands[cur][0]), cands[cur][1])
        print(f"\npage {pg+1} card {ci+1}: CURRENT->{cur} (score {nc[0]}, conf {nc[1]:.1f})"
              f"   PROPOSED->{pro} (labels {labels_only(cands[pro][0])}, conf {cands[pro][1]:.1f})")
        print(f"   CURRENT  fields: {fc}")
        print(f"   PROPOSED fields: {fp}")
        print(f"   cur lines : {[l[:60] for l in cands[cur][0][:4]]}")
        print(f"   pro lines : {[l[:60] for l in cands[pro][0][:4]]}")
print(f"\ntotal flips: {flips} of 120 cards")