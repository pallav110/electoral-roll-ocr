"""Can the section line be FOUND rather than GUESSED?

The measured failure is positional: on S28-40 the printed section line occupies
1.96-3.05% of page height, on S24-53 it sits lower relative to the page, and
every constant in the code -- the y-fractions inside the header window, the
absolute 40pt and 52pt clips -- was fitted to one of them.

The alternative is to stop assuming a position.  Detect the ink bands in the
top strip, read each band on its own, and let the printed label identify which
band is the section line.  That is scale-free: a band is a band whatever height
the page is, and the label is the same on every roll.

This measures whether that approach actually recovers both rolls before any
production code is changed.
"""
import sys

sys.stdout = open("/data/locate_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

CASES = (
    ("S28-40 Lansdown", "/data/roll107.pdf", "कोचियार"),
    ("S24-53 works", "/data/roll_s24_300.pdf", "उत्तरांचल कालोनी गली न0 8 से 9"),
)


def bands(gray: np.ndarray, gap: int, min_px: int) -> list[tuple[int, int]]:
    dark = (gray < 140).sum(axis=1)
    rows = np.where(dark > 2)[0]
    if not len(rows):
        return []
    groups: list[list[int]] = [[rows[0]]]
    for r in rows[1:]:
        if r - groups[-1][-1] <= gap:
            groups[-1].append(r)
        else:
            groups.append([r])
    return [(g[0], g[-1]) for g in groups if g[-1] - g[0] >= min_px]


bad = 0
for label, path, want_name in CASES:
    doc = fitz.open(path)
    page = doc[2]
    H = page.rect.height
    print("=" * 78)
    print(f"{label}   page {page.rect.width:.0f}x{H:.0f}pt   want name {want_name!r}")
    print("=" * 78)

    # Render the top strip once.  8% is wide enough to contain the section line
    # on both measured rolls (it starts by 3.05%) and still far above the cards.
    strip_h = H * 0.08
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + strip_h)
    raw = np.frombuffer(page.get_pixmap(dpi=200, clip=clip,
                                        alpha=False).tobytes("png"), np.uint8)
    im = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    sh = gray.shape[0]
    print(f"  strip = top 8% ({strip_h:.0f}pt) -> {gray.shape[1]}x{sh}px; "
          f"1% of page = {sh/8:.1f}px")

    # The gap threshold scales with the render, so bands merge or split the same
    # way on a 2806pt page and a 842pt one.
    gap = max(4, sh // 100)
    found = None
    for y0, y1 in bands(gray, gap, min_px=sh // 200):
        seg = gray[y0:y1 + 1, :]
        cols = np.where((seg < 140).sum(axis=0) > 0)[0]
        if not len(cols):
            continue
        crop = seg[:, cols[0]:cols[-1] + 1]
        # Scale to a readable height, never down -- the idiom the house/EPIC
        # readers already use, so the band reads the same at any page size.
        scale = max(1, -(-A.TESSERACT_MIN_HEIGHT_PX // max(1, crop.shape[0])))
        if scale > 1:
            crop = cv2.resize(crop, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_CUBIC)
        text = " ".join(pytesseract.image_to_string(
            Image.fromarray(crop), lang="hin", config="--oem 3 --psm 7").split())
        p0, p1 = y0 / sh * 8, y1 / sh * 8
        mark = ""
        if "अनुभाग" in text:
            mark = "   <== SECTION LINE"
            found = (text, crop)
        print(f"    {p0:5.2f}%-{p1:5.2f}%  h={y1-y0+1:>3}px "
              f"x{scale}  {text[:70]!r}{mark}")

    if found is None:
        bad += 1
        print("    NO BAND CARRIED THE अनुभाग LABEL")
        continue

    text, crop = found
    after = A._split_anubhag_label(text)
    code = A._anubhag_code_from_header_text(after)
    name = A._clean_anubhag_name_candidate(text)
    for field, got, want in (("code", code, "1"), ("name", name, want_name)):
        ok = got == want
        bad += not ok
        print(f"\n    {'OK  ' if ok else 'FAIL'} {field} = {got!r}")
        if not ok:
            print(f"         want {want!r}")
    print(f"         review reason = "
          f"{A._select_anubhag_name(text)[1]!r}")
    print()

print("=" * 78)
print("LOCATED CORRECTLY ON BOTH ROLLS" if not bad else f"{bad} PROBLEM(S)")
print("=" * 78)