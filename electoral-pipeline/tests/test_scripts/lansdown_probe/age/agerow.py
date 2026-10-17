"""Read the age digits off the row they are printed on, not a guessed box.

`_age_value_span` takes the region between the आयु and लिंग word boxes.  On the
S24 template that box holds the digits.  On the S28-40 template the labels sit at
different heights, so the box contains only the shirorekha joining them -- the
rendered crop is a 136x65 sliver of the rule, with no digits on it at all.

So instead of a box between two labels, take the INK BAND that carries the
age label and read the digits to its right on that same band.  A band is found
from the pixels rather than from a template, which is why it survives a layout
whose labels moved.

Both approaches are measured here against the printed values, so the fix is
chosen on evidence rather than on plausibility.
"""
import sys

sys.stdout = open("/data/agerow.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import re
import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
paddle = A._get_paddle()

TRUTH = {0: "41", 1: "37", 3: "68", 4: "88", 5: "79", 6: "48", 7: "49"}
DEVA = str.maketrans("०१२३४५६७८९", "0123456789")


def ink_bands(gray, gap):
    """Row bands carrying ink, gaps scaled to the image so they survive resize."""
    dark = (gray < 150).sum(axis=1)
    rows = np.where(dark > 1)[0]
    if not len(rows):
        return []
    out, cur = [], [int(rows[0])]
    for r in rows[1:]:
        r = int(r)
        if r - cur[-1] <= gap:
            cur.append(r)
        else:
            out.append(cur)
            cur = [r]
    out.append(cur)
    return [(g[0], g[-1]) for g in out]


def digits_on_band(gray, y0, y1, left_frac=0.0, right_frac=0.55):
    """Read digits from the right part of one band."""
    h, w = gray.shape
    band = gray[max(0, y0 - 2):min(h, y1 + 3), int(w * left_frac):int(w * right_frac)]
    if band.size == 0:
        return ""
    up = cv2.resize(band, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    _, otsu = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    best = ""
    for img in (up, otsu):
        for psm in (7, 8, 13):
            try:
                txt = pytesseract.image_to_string(
                    Image.fromarray(img), lang="eng",
                    config=(f"--oem 3 --psm {psm} "
                            "-c tessedit_char_whitelist=0123456789"))
            except Exception:
                continue
            runs = [d for d in re.findall(r"\d{2,3}", txt.translate(DEVA))
                    if 18 <= int(d) <= 120]
            if runs:
                cand = max(runs, key=lambda x: (len(x), int(x)))
                if len(cand) > len(best) or not best:
                    best = cand
    return best


hits = 0
for idx, truth in TRUTH.items():
    r = rects[idx]
    card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    clean = A._remove_outer_card_lines(card)
    if clean is None or getattr(clean, "size", 0) == 0:
        clean = card
    crop = A._crop_card_region(clean, A.AGE_REGION, "AGE")
    if crop is None or getattr(crop, "size", 0) == 0:
        continue
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h = gray.shape[0]

    # Bands with a gap proportional to the crop, so the same thresholds hold at
    # 186px and at 60px.
    gap = max(2, h // 22)
    got = ""
    chosen = None
    for y0, y1 in ink_bands(gray, gap):
        band = gray[y0:y1 + 1, :]
        cols = np.where((band < 150).sum(axis=0) > 0)[0]
        if not len(cols):
            continue
        left = band[:, cols[0]:int(gray.shape[1] * 0.45)]
        up = cv2.resize(left, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        try:
            txt = " ".join(pytesseract.image_to_string(
                Image.fromarray(up), lang="hin+eng",
                config="--oem 3 --psm 7").split())
        except Exception:
            continue
        is_age = bool(A.AGE_LABEL_RE.search(txt))
        value = digits_on_band(gray, y0, y1)
        mark = ""
        if is_age and value:
            mark = "   <== AGE"
            chosen = value
        print(f"  card {idx} band {y0:>3}-{y1:<3} label={'yes' if is_age else 'no ':>3} "
              f"digits={value!r:6} {txt[:44]!r}{mark}")
    if chosen:
        hits += chosen == truth
        print(f"    -> {chosen!r}  truth {truth!r}  "
              f"{'MATCH' if chosen == truth else 'WRONG'}")
    else:
        print(f"    -> nothing    truth {truth!r}  WRONG")
    print()

print(f"band-row reading matches printed value on {hits}/{len(TRUTH)} cards")