"""Phase-1 evidence for the anubhag failure.

Question this answers, with measurements rather than assumptions:
  1. Where does the printed section line ACTUALLY sit, as a fraction of page
     height, on each roll?
  2. Does the CURRENT crop window (min(52.0, H) / min(40.0, H)) contain any ink
     at all, or is it geometrically empty?
  3. What does the printed text say, read from a window sized to contain it?

(2) is the decisive one.  If the crop is empty, no amount of preprocessing,
psm mode or threshold tuning can help -- the pixels are simply not there.
"""
import sys

sys.stdout = open("/data/anub_geom_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import pytesseract
import ocr_pdf_api as A

CASES = (
    ("S28-40 Lansdown", "/data/roll107.pdf", {"anubhag_code": "1"}),
    ("S24-53 works", "/data/roll_s24_300.pdf", {"anubhag_code": "1"}),
)


def ink_rows(gray: np.ndarray, thresh: int = 140) -> list[tuple[int, int]]:
    """Group rows that contain dark pixels into (start, end) bands."""
    dark = (gray < thresh).sum(axis=1)
    rows = np.where(dark > 2)[0]
    if not len(rows):
        return []
    bands: list[list[int]] = [[rows[0]]]
    for r in rows[1:]:
        if r - bands[-1][-1] <= 8:
            bands[-1].append(r)
        else:
            bands.append([r])
    return [(b[0], b[-1]) for b in bands]


for label, path, truth in CASES:
    doc = fitz.open(path)
    page = doc[2]                      # the pipeline reads page 3
    H, W = page.rect.height, page.rect.width
    print("=" * 78)
    print(f"{label}   page {W:.0f}x{H:.0f}pt   truth={truth}")
    print("=" * 78)

    # ---- 1. Where is the text? -------------------------------------------
    # Render the top 12% of the page and find the ink bands inside it.
    band = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + H * 0.12)
    raw = np.frombuffer(page.get_pixmap(dpi=200, clip=band,
                                        alpha=False).tobytes("png"), np.uint8)
    im = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    bh = gray.shape[0]
    print(f"\n  top 12% rendered = {gray.shape[1]}x{bh}px  (1% of page = {bh/12:.1f}px)")

    print("\n  ink bands in the top 12%, as % of PAGE height:")
    for y0, y1 in ink_rows(gray):
        p0, p1 = y0 / bh * 12, y1 / bh * 12
        seg = gray[y0:y1 + 1, :]
        cols = np.where((seg < 140).sum(axis=0) > 0)[0]
        crop = seg[:, cols[0]:cols[-1] + 1] if len(cols) else seg
        up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        text = " ".join(pytesseract.image_to_string(
            Image.fromarray(up), lang="hin+eng",
            config="--oem 3 --psm 7").split())
        print(f"    {p0:5.2f}% - {p1:5.2f}%   {text[:88]!r}")

    # ---- 2. Does the CURRENT crop contain any ink? -----------------------
    # Two readers use an absolute cap: the code reader at line 6493 and the
    # name reader at line 6613.  Count dark pixels inside each.
    print("\n  CURRENT absolute-cap crops -- ink inside?")
    for fn_name, pts in (("code reader L6493", 52.0),
                         ("name reader L6613", 40.0)):
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                         page.rect.y0 + min(pts, H))
        r = np.frombuffer(page.get_pixmap(dpi=200, clip=clip,
                                          alpha=False).tobytes("png"), np.uint8)
        g = cv2.cvtColor(cv2.imdecode(r, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
        dark = int((g < 140).sum())
        print(f"    {fn_name:20} top {min(pts,H):5.1f}pt = {min(pts,H)/H*100:5.2f}% "
              f"of page   dark pixels = {dark:>7}   "
              f"{'EMPTY -- text is below this line' if dark == 0 else 'has ink'}")

    # ---- 3. Read the line from a window sized to contain it ---------------
    # Take the widest plausible band, then let the printed label find itself.
    wide = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + H * 0.09)
    r = np.frombuffer(page.get_pixmap(dpi=300, clip=wide,
                                      alpha=False).tobytes("png"), np.uint8)
    wim = cv2.imdecode(r, cv2.IMREAD_COLOR)
    print(f"\n  read from top 9% ({H*0.09:.0f}pt, {wim.shape[1]}x{wim.shape[0]}px):")
    for psm in (6, 11, 4):
        text = pytesseract.image_to_string(Image.fromarray(wim), lang="hin",
                                           config=f"--oem 3 --psm {psm}")
        hit = [ln for ln in text.splitlines()
               if "अनुभाग" in ln and "नाम" in ln]
        print(f"    psm {psm:>2}: {len(hit)} labelled line(s)")
        for ln in hit:
            after = A._split_anubhag_label(" ".join(ln.split()))
            print(f"       raw    = {ln.strip()[:88]!r}")
            print(f"       after  = {after!r}")
            print(f"       code   = {A._anubhag_code_from_header_text(after)!r}")
            print(f"       cleaned= {A._clean_anubhag_name_candidate(ln)!r}")
    print()