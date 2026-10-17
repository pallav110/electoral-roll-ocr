"""Is the wrong age (88 for a printed 41) caused by the upscale removal?

Card 0 of the Lansdown roll prints 'आयु : 41'.  After removing the 2x upscale the
pipeline returns 88.  Whether that is a regression or a pre-existing fault is
the question, and it decides whether the speed fix is safe to keep.

This runs the SAME cards with the upscale forced back on -- by patching the
module's scale computation, not by editing it -- and compares age, house, name
and gender field by field.  A pre-existing fault means the fix is innocent and
the age reader needs its own work; a regression means the fix must be reworked.
"""
import sys
import time

sys.stdout = open("/data/age_cmp.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
FIELDS = ("age", "house_no", "id_card_no", "voter_first_name",
          "voter_sur_name", "relation_name", "gender", "voter_father_name",
          "voter_husband_name", "is_deleted")

# Ground truth read off the rendered cards, page 4.
TRUTH = {
    0: {"age": "41", "house_no": "7", "voter_first_name": "गीता",
        "voter_sur_name": "देवी", "relation_name": "पति",
        "voter_father_name": "नरेंद्र सिंह", "gender": "महिला"},
    1: {"age": "37", "house_no": "7", "voter_first_name": "गुडडी",
        "voter_sur_name": "देवी", "relation_name": "पिता",
        "voter_father_name": "महेन्द्र सिंह", "gender": "पुरुष"},
    3: {"age": "68", "house_no": "8", "voter_first_name": "धीरू",
        "voter_sur_name": "देवी", "relation_name": "पति",
        "voter_father_name": "मनवर सिंह", "gender": "महिला"},
}


def records(path, page_no, indices):
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    out = []
    for i in indices:
        rec = A._extract_card(page, rects[i], page_no + 1, i)
        out.append({f: (rec.get(f) if rec else None) for f in FIELDS})
    doc.close()
    return out


INDICES = sorted(TRUTH)
doc = fitz.open("/data/roll107.pdf")

print("=" * 74)
print("CURRENT (no upscale)")
print("=" * 74)
new = records("/data/roll107.pdf", 3, INDICES)

# Force the upscale by lowering the minimum-height bar so scale becomes 2 on a
# 689px card: scale = ceil(bar / 689), which is 2 when bar > 689.
saved = A.TESSERACT_MIN_HEIGHT_PX
A.TESSERACT_MIN_HEIGHT_PX = saved + 1
scale_now = -(-A.TESSERACT_MIN_HEIGHT_PX // 689)
print(f"  forced scale for a 689px card: {scale_now}")

# Force the 2x path explicitly by temporarily raising the bar a lot.
A.TESSERACT_MIN_HEIGHT_PX = 1379          # ceil(1379/689) == 2
old = records("/data/roll107.pdf", 3, INDICES)
A.TESSERACT_MIN_HEIGHT_PX = saved
print(f"  scale with bar=1379: {-(-1379 // 689)}")

print("\n" + "=" * 74)
print("GROUND TRUTH vs BOTH PATHS")
print("=" * 74)
for n, idx in enumerate(INDICES):
    print(f"\n  card {idx}")
    for f in ("age", "house_no", "voter_first_name", "relation_name",
              "voter_father_name", "gender"):
        t = TRUTH[idx].get(f, "")
        a, b = new[n][f], old[n][f]
        mark_a = "OK " if str(a) == t else "WRONG"
        mark_b = "OK " if str(b) == t else "WRONG"
        print(f"    {f:20} truth={str(t):18} "
              f"no-upscale={str(a):18} {mark_a:5} "
              f"upscale={str(b):18} {mark_b}")

print("\n" + "=" * 74)
print("FIELD-LEVEL DIFF BETWEEN THE TWO PATHS")
print("=" * 74)
diffs = {}
for n, idx in enumerate(INDICES):
    for f in FIELDS:
        if new[n][f] != old[n][f]:
            diffs.setdefault(f, []).append(
                (idx, new[n][f], old[n][f], TRUTH[idx].get(f, "-")))
if not diffs:
    print("  no differences at all -- the upscale change is inert for these cards")
for f, rows in diffs.items():
    print(f"  {f}: {len(rows)} difference(s)")
    for idx, a, b, t in rows:
        which = ("no-upscale" if str(a) == str(t) else
                 "upscale" if str(b) == str(t) else "neither")
        print(f"    card {idx}: no-upscale={a!r} upscale={b!r} truth={t!r} "
              f"-> correct: {which}")