"""Verify the speed fix changed cost without changing what was read.

Two questions the live numbers cannot answer on their own:

  1. Does the early exit fire?  `hindi_text` stayed at 746 calls for 120 cards
     both before and after, so it may not be.  If the label score sits below
     HINDI_READ_ACCEPT_LABEL_SCORE on most cards, the second read still runs and
     the constant is wrong.

  2. Are the extracted VALUES the same?  Record counts were identical (120), but
     a count cannot detect a changed name.  This compares every field of every
     record between the old 2x/2-read behaviour and the new one.
"""
import sys
import time
import json

sys.stdout = open("/data/verify.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
CASES = (("Lansdown", "/data/roll107.pdf", 3), ("S24-53", "/data/roll_s24_300.pdf", 3))

FIELDS = ("id_card_no", "voter_first_name", "voter_middle_name",
          "voter_sur_name", "relation_name", "voter_father_name",
          "voter_husband_name", "voter_mother_name", "voter_other_name",
          "house_no", "age", "gender", "is_deleted")


def cards_of(path, page_no, n=10):
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    out = [raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy() for r in rects[:n]]
    doc.close()
    return out


print("=" * 74)
print("1. DOES THE EARLY EXIT FIRE?  (label score distribution)")
print("=" * 74)
for label, path, page_no in CASES:
    scores = []
    for card in cards_of(path, page_no):
        lines, _conf = A._extract_text_with_tesseract(
            cv2.imencode(".png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))[1].tobytes())
        scores.append(A._hindi_line_score(lines or []))
    accept = A.HINDI_READ_ACCEPT_LABEL_SCORE
    passing = sum(1 for s in scores if s >= accept)
    print(f"  {label}: bar={accept}")
    print(f"    scores {scores}")
    print(f"    {passing}/{len(scores)} cards would skip the second read")

print()
print("=" * 74)
print("2. ARE THE EXTRACTED VALUES THE SAME?")
print("=" * 74)

for label, path, page_no in CASES:
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)[:10]
    print(f"\n  {label}   {len(rects)} cards")

    new = [A._extract_card(page, r, page_no + 1, i)
           for i, r in enumerate(rects)]

    # Reproduce the OLD behaviour by forcing the upscale and both variants, via
    # a temporary patch of the module's accept bar to 0 -- which disables the
    # early exit -- while the upscale is restored separately.  The upscale is
    # patched by monkeypatching TESSERACT_MIN_HEIGHT_PX high enough that
    # scale == 2 exactly as before.
    old_scale = A.TESSERACT_MIN_HEIGHT_PX
    A.TESSERACT_MIN_HEIGHT_PX = old_scale * 2      # forces the 2x path
    A.HINDI_READ_ACCEPT_LABEL_SCORE = 0             # forces both reads
    try:
        old = [A._extract_card(page, r, page_no + 1, i)
               for i, r in enumerate(rects)]
    finally:
        A.TESSERACT_MIN_HEIGHT_PX = old_scale
        A.HINDI_READ_ACCEPT_LABEL_SCORE = 60

    same = diff = 0
    field_diffs = {}
    for a, b in zip(new, old):
        if a is None or b is None:
            if a is not b:
                diff += 1
            else:
                same += 1
            continue
        card_same = True
        for f in FIELDS:
            va, vb = a.get(f), b.get(f)
            if va != vb:
                field_diffs.setdefault(f, []).append((va, vb))
                card_same = False
        same += card_same
        diff += not card_same

    print(f"    identical cards: {same}/{len(new)}   differing: {diff}")
    for f, pairs in field_diffs.items():
        print(f"    {f}: {len(pairs)} difference(s)")
        for va, vb in pairs[:4]:
            print(f"        new={va!r}  old={vb!r}")
    doc.close()

print()
print("=" * 74)
print("3. TIMING, same cards")
print("=" * 74)
for label, path, page_no in CASES:
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)[:10]
    t = time.perf_counter()
    for r in rects:
        A._extract_card(page, r, page_no + 1, 0)
    new_s = time.perf_counter() - t

    old_scale = A.TESSERACT_MIN_HEIGHT_PX
    A.TESSERACT_MIN_HEIGHT_PX = old_scale * 2
    A.HINDI_READ_ACCEPT_LABEL_SCORE = 0
    try:
        t = time.perf_counter()
        for r in rects:
            A._extract_card(page, r, page_no + 1, 0)
        old_s = time.perf_counter() - t
    finally:
        A.TESSERACT_MIN_HEIGHT_PX = old_scale
        A.HINDI_READ_ACCEPT_LABEL_SCORE = 60

    print(f"  {label}: old {old_s:.2f}s -> new {new_s:.2f}s "
          f"({old_s / new_s:.2f}x)  for {len(rects)} cards")
    doc.close()