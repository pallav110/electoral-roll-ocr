import sys, json, time
sys.stdout = open("/data/hdr_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2, re
from PIL import Image
import pytesseract
import ocr_pdf_api as A

CASES = (("S28-40 Lansdown", "/data/roll107.pdf", {"ac": "40", "booth": "107", "anubhag": "1"}),
         ("S24-53 works", "/data/roll_s24_300.pdf", {"ac": "53", "booth": "300", "anubhag": "1"}))

for label, path, truth in CASES:
    print("#" * 76); print(f"# {label}  truth={truth}"); print("#" * 76)
    d = fitz.open(path); page = d[2]
    H = page.rect.height
    print(f"page {page.rect.width:.0f}x{H:.0f}pt; header clip now "
          f"{max(52.0, H*0.10):.0f}pt = {max(52.0, H*0.10)/H*100:.2f}%")

    t0 = time.time()
    md = A._extract_roll_header_metadata(page)
    print(f"\n_extract_roll_header_metadata ({time.time()-t0:.1f}s):")
    print("  ac_code      =", repr(md["ac_code"]))
    print("  booth_code   =", repr(md["booth_code"]))
    print("  anubhag_code =", repr(md["anubhag_code"]))
    print("  anubhag_name =", repr(md["anubhag_name"]))
    print("  review       =", repr(md["anubhag_review_reason"]))
    print("  raw name reads:", json.dumps(md.get("anubhag_name_raw_reads", [])[:5], ensure_ascii=False))
    print("  cleaned      :", json.dumps(md.get("anubhag_name_candidates", [])[:5], ensure_ascii=False))

    # ---- direct look at the crops ----
    clip = A.fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + max(52.0, H*0.10))
    img = np.frombuffer(page.get_pixmap(dpi=300, clip=clip, alpha=False).tobytes("png"), np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    h, w = im.shape[:2]
    print(f"\n  header render: {w}x{h}")
    def sub(x0, y0, x1, y1, scale=3, tag=""):
        r = im[int(h*y0):int(h*y1), int(w*x0):int(w*x1)]
        r = cv2.resize(r, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        cv2.imwrite(f"/data/hdr_{label.split()[0]}_{tag}.png", r)
        g = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY)
        t = A._tess_string(Image.fromarray(g), lang="eng",
                           config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789")
        print(f"   {tag:10} size={r.shape[:2]} digit_read={t.strip()!r}")
        return g
    sub(0.282, 0.06, 0.363, 0.39, 3, "ac")
    sub(0.847, 0.06, 0.984, 0.39, 3, "booth")
    g = sub(0.120, 0.33, 0.500, 0.68, 1.8, "name")
    otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    ad = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9)
    for nm, v in (("name_gray", g), ("name_otsu", otsu), ("name_adapt", ad)):
        for psm in (7, 6, 13):
            t = pytesseract.image_to_string(Image.fromarray(v), lang="hin",
                                            config=f"--oem 3 --psm {psm}")
            print(f"     {nm:10} psm{psm:>2}: {A._split_anubhag_label(' '.join(t.split()))!r}")
    print()