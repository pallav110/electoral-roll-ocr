"""Is `age` still correct after the upscale removal, and where does 14s go?

Two open questions from the speed work:

  1. An earlier verification reported one card's age changing from 33 to 88.
     Verification's "old" path did not actually reproduce the upscale (it forced
     TESSERACT_MIN_HEIGHT_PX, which leaves scale at 1 for a 689px card), so that
     comparison was early-exit-only and the 33/88 pair is not yet explained.
     Age is the field the project has least defence for -- no vote, no engine
     attribution, no review flag -- so it is checked against the printed card.

  2. The page takes 23.96s.  Paddle is 7.66s of that and the card pool ~1.8s.
     About 14s is unattributed.  This times every reader `_extract_card` calls,
     in isolation, so the remainder is named rather than guessed.
"""
import sys
import time

sys.stdout = open("/data/age.txt", "w", encoding="utf-8", errors="replace")
sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import ocr_pdf_api as A

SCALE = 200 / 72.0
CASES = (("Lansdown", "/data/roll107.pdf", 3, 8),
         ("S24-53", "/data/roll_s24_300.pdf", 3, 8))

print("=" * 74)
print("1. AGE -- printed value vs extracted value, with the card rendered")
print("=" * 74)

for label, path, page_no, n in CASES:
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    print(f"\n  {label}  page {page_no + 1}")
    for idx in range(n):
        r = rects[idx]
        card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
                   int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
        rec = A._extract_card(page, r, page_no + 1, idx)
        age = rec.get("age") if rec else None
        cv2.imwrite(f"/data/ag_{label.split()[0]}_{idx}.png",
                    cv2.cvtColor(card, cv2.COLOR_RGB2BGR))
        print(f"    card {idx}: age={age!r:>8}  "
              f"gender={rec.get('gender') if rec else None!r:>10}  "
              f"-> ag_{label.split()[0]}_{idx}.png")
    doc.close()

print()
print("=" * 74)
print("2. TIME EVERY READER _extract_card CALLS")
print("=" * 74)

doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
cards = [raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
             int(r.x0 * SCALE):int(r.x1 * SCALE)].copy() for r in rects]
A._get_paddle()

cleaned = []
for c in cards:
    cl = A._remove_outer_card_lines(c)
    cleaned.append(cl if cl is not None and getattr(cl, "size", 0) else c)


def bench(label, fn, items):
    t = time.perf_counter()
    for i in items:
        fn(i)
    el = time.perf_counter() - t
    print(f"  {label:34} {el:7.2f}s  ({el / len(items):6.3f}s each)", flush=True)
    return el


def enc_bgr(c):
    return cv2.imencode(".png", cv2.cvtColor(c, cv2.COLOR_RGB2BGR))[1].tobytes()


bench("remove_outer_card_lines", lambda c: A._remove_outer_card_lines(c), cards)
bench("hindi reader (2 reads)",
      lambda c: A._extract_text_with_tesseract(enc_bgr(c)), cards)
bench("serial reader",
      lambda c: A._extract_serial_with_tesseract(enc_bgr(c)), cards[:10])
bench("epic reader",
      lambda c: A._extract_epic_with_tesseract(enc_bgr(c)), cards[:10])
bench("house both",
      lambda c: A._extract_house_number_with_tesseract_both(
          A._crop_card_region(c, A.HOUSE_REGION, "H")), cards[:10])


def house_line(c):
    cr = A._crop_card_region(c, (0.12, 0.46, 0.99, 0.64), "HOUSE_ENGLISH")
    if cr is None or getattr(cr, "size", 0) == 0:
        return ""
    return A._tess_string(
        Image.fromarray(cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY)),
        lang="eng", config="--oem 3 --psm 7")


bench("house english line (30)", house_line, cards)


def gender_read(c):
    return A._extract_gender_from_clean_card(c)


bench("gender reader", gender_read, cards[:10])

paddle = A._get_paddle()


def stacked(clean):
    info = []
    ser = A._crop_card_region(clean, A.TESSERACT_CARD_CONTENT_REGIONS[0], "SERIAL")
    if ser is not None and getattr(ser, "size", 0):
        s = min(6, max(2, 80 // max(1, ser.shape[0])))
        info.append(cv2.resize(ser, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC))
    hv = A._crop_card_region(clean, A.HOUSE_REGION, "HOUSE")
    if hv is not None and getattr(hv, "size", 0):
        s = min(6, max(2, 80 // max(1, hv.shape[0])))
        info.append(cv2.resize(hv, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC))
    if not info:
        return None
    mw = max(c.shape[1] for c in info)
    pad = [cv2.copyMakeBorder(c, 4, 4, 4, mw - c.shape[1] + 4,
                              cv2.BORDER_CONSTANT, value=(255, 255, 255))
           for c in info]
    st = np.vstack(pad) if len(pad) > 1 else pad[0]
    try:
        paddle.ocr(st, cls=False)
    except Exception:
        return None
    return st


bench("paddle stacked pass", stacked, cleaned)


def vote(c):
    ser = A._crop_card_region(c, A.TESSERACT_CARD_CONTENT_REGIONS[0], "SERIAL")
    if ser is None or getattr(ser, "size", 0) == 0:
        return False
    return A._serial_deleted_vote(ser)


bench("paddle deleted vote", vote, cards)
print("\ndone")