import sys, json, time
sys.stdout = open("/data/debug3_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2, re
from PIL import Image
import pytesseract
import ocr_pdf_api as A

CASES = (("S28-40 Lansdown", "/data/roll107.pdf", {"booth": "107", "ac": "40"}),
         ("S24-53 works", "/data/roll_s24_300.pdf", {"booth": "300", "ac": "53"}))

for label, path, truth in CASES:
    print("#" * 76); print(f"# {label}   truth={truth}"); print("#" * 76)
    d = fitz.open(path)
    page3 = d[2]                      # <-- the pipeline reads page 3, not the cover
    page1 = d[0]

    # ---------- ISSUE 2: header metadata on the CORRECT page ----------
    print("\n--- ISSUE 2: _extract_roll_header_metadata(page 3) ---")
    t0 = time.time()
    md = A._extract_roll_header_metadata(page3)
    print(f"({time.time()-t0:.1f}s)")
    print("  ", json.dumps({k: v for k, v in md.items()
                           if k not in ("anubhag_name_candidates", "anubhag_name_raw_reads")},
                          ensure_ascii=False, default=str))
    print("   raw reads:", json.dumps(md.get("anubhag_name_raw_reads", [])[:4],
                                      ensure_ascii=False))

    # what text IS on page 3's header band?
    print("\n   page-3 header band, raw OCR:")
    H, W = page3.rect.height, page3.rect.width
    band = fitz.Rect(page3.rect.x0, page3.rect.y0, page3.rect.x1, page3.rect.y0 + H * 0.09)
    img = np.frombuffer(page3.get_pixmap(dpi=300, clip=band, alpha=False).tobytes("png"), np.uint8)
    im = cv2.imdecode(img, cv2.IMREAD_COLOR)
    print("     ", " ".join(pytesseract.image_to_string(
        Image.fromarray(im), lang="hin+eng", config="--oem 3 --psm 6").split())[:300])

    # ---------- ISSUE 1: booth code ----------
    print("\n--- ISSUE 1: booth_code ---")
    for nm, pg, src in (("cover page1", page1, "cover"), ("header page3", page3, "p3")):
        try:
            v = A._extract_booth_code_from_cover(pg)
            print(f"   _extract_booth_code_from_cover({nm}) -> {v!r}")
        except Exception as e:
            print(f"   {nm} raised {type(e).__name__}: {e}")

    # ---------- ISSUE 3: relation names ----------
    print("\n--- ISSUE 3: relation names ---")
    import numpy as np2
    SCALE = 200 / 72.0
    pix = page3.get_pixmap(dpi=200)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    rel_hit = 0
    for ci, r in enumerate(A._voter_card_rects(page3)[:6]):
        crop = raw[int(r.y0*SCALE):int(r.y1*SCALE), int(r.x0*SCALE):int(r.x1*SCALE)].copy()
        ok, buf = cv2.imencode(".png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
        lines, conf = A._extract_text_with_tesseract(buf.tobytes())
        rec = A.parse_voter_box_from_ocr_lines(lines or [])
        has_type = bool(rec.get("relation_name"))
        has_val = any(rec.get(k) for k in ("voter_father_name", "voter_husband_name",
                                           "voter_mother_name", "voter_other_name"))
        rel_hit += has_val
        print(f"   card {ci+1}: relation_name={rec.get('relation_name')!r} "
              f"value={rec.get('voter_father_name') or rec.get('voter_husband_name') or ''!r} "
              f"| first 3 lines: {(lines or [])[:3]}")
    print(f"   -> relation VALUE filled on {rel_hit}/6 cards")
    print()