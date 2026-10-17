"""End-to-end verification of the label-separator fix, on the real PDFs.

Calls the live `_extract_roll_header_metadata` (the function the pipeline
actually runs, on page 3) for both rolls and reports every header field.  The
fix must move anubhag_code and anubhag_name from blank to correct on Lansdown
while leaving ac_code, booth_code and every S24 field exactly as they were.
"""
import sys
import json
import time

sys.stdout = open("/data/anubhag_e2e.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import ocr_pdf_api as A

CASES = (
    ("S28-40 Lansdown", "/data/roll107.pdf",
     {"ac_code": "40", "booth_code": "107", "anubhag_code": "1",
      "anubhag_name": "कोचियार"}),
    # S24-53's section number is 1, confirmed by the user.  Its header READS as
    # 'अनुभाग संख्या और नाम : 4-उत्तरांचल कालोनी गली न0 8 से 9' because
    # Tesseract's Hindi model renders a printed 1 as 4 on this roll -- the
    # confusion this module documents.  The English-only reader of the same band
    # returns 1 at all 12 resolution/psm variants tried, and Paddle returns ['1',
    # '53', '1'] on the same pixels.
    ("S24-53 works", "/data/roll_s24_300.pdf",
     {"ac_code": "53", "booth_code": "300", "anubhag_code": "1",
      "anubhag_name": "उत्तरांचल कालोनी गली न0 8 से 9"}),
)

bad = 0
for label, path, truth in CASES:
    doc = fitz.open(path)
    page = doc[2]
    t0 = time.time()
    md = A._extract_roll_header_metadata(page)
    elapsed = time.time() - t0

    print("=" * 78)
    print(f"{label}   ({elapsed:.1f}s)")
    print("=" * 78)
    for key in ("ac_code", "booth_code", "anubhag_code", "anubhag_name"):
        got = md.get(key, "")
        want = truth.get(key, "")
        ok = got == want
        bad += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {key:13} = {got!r}")
        if not ok:
            print(f"                    want {want!r}")
    print(f"       review       = {md.get('anubhag_review_reason', '')!r}")
    print(f"       raw reads    = "
          f"{json.dumps(md.get('anubhag_name_raw_reads', []), ensure_ascii=False)}")

    # The per-page fallbacks, which are separate readers and must not regress.
    print(f"       name fallback= "
          f"{A._extract_anubhag_name_from_header_fallback(page)!r}")
    print(f"       code fallback= "
          f"{A._extract_anubhag_code_from_header_fallback(page)!r}")

print("=" * 78)
print(f"{'ALL FIELDS CORRECT' if not bad else str(bad) + ' FIELD(S) WRONG'}")
print("=" * 78)