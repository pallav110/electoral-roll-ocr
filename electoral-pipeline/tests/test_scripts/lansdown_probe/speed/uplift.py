"""Does the 2x upscale in the Hindi reader earn its cost?

`_extract_text_with_tesseract` upscales the card 2x in both dimensions -- 4x
the pixels -- and then reads TWICE on it (gray + adaptiveThreshold).  So each
card pays 2 reads at 4x area.

Measured earlier: cost is nearly flat against pixels (8% of the area still costs
61% of the time), which suggests a large fixed per-call cost.  But that was
measured on a downscaled ORIGINAL.  Here the question is different: at 4x area
the LSTM's convolutional pass may become genuinely expensive, and the upscale is
the one thing in the hot path that inflates pixels.

This compares, on real cards from both rolls:
  * 1.0x, one read  -- the floor
  * 1.0x, two reads -- what removing the threshold variant would save
  * 2.0x, two reads -- today's actual cost
and reports ACCURACY alongside time, because dropping a variant is only safe if
the surviving read is at least as good.  `_hindi_line_score` is used for the
comparison, so this measures exactly what the pipeline ranks on.
"""
import sys
import time

sys.stdout = open("/data/uplift.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import ocr_pdf_api as A

SCALE = 200 / 72.0


def cards_of(path, page_no, n=6):
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    out = []
    for r in rects[:n]:
        out.append(raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
                        int(r.x0 * SCALE):int(r.x1 * SCALE)].copy())
    doc.close()
    return out


def read(gray):
    """One _tess_data read, exactly as the pipeline does it."""
    data = A._tess_data(Image.fromarray(gray),
                        lang=A.TESSERACT_HINDI_LANG, config=A.TESSERACT_CONFIG)
    lines, confs = [], []
    key = None
    buf = []
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        k = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if k != key:
            if buf:
                lines.append(" ".join(buf))
                confs.append(sum(confs and [-1] or [0]) if False else 0)
            buf, key, confs = [], k, []
        buf.append(text)
        try:
            confs.append(float(data["conf"][i]))
        except (TypeError, ValueError):
            confs.append(-1.0)
    if buf:
        lines.append(" ".join(buf))
    mean = float(np.mean([c for c in confs if c >= 0])) if any(
        c >= 0 for c in confs) else 0.0
    return lines, mean


CASES = (("Lansdown", "/data/roll107.pdf", 3), ("S24-53", "/data/roll_s24_300.pdf", 3))

for label, path, page_no in CASES:
    cards = cards_of(path, page_no)
    print("=" * 74)
    print(f"{label}   {len(cards)} cards   card {cards[0].shape}")
    print("=" * 74)

    rows = {}
    for upscale, nvars in ((1.0, 1), (1.0, 2), (2.0, 2)):
        key = f"{upscale:g}x / {nvars} read{'s' if nvars > 1 else ''}"
        t = time.perf_counter()
        scores, texts = [], []
        for card in cards:
            img = card
            if upscale != 1.0:
                img = cv2.resize(card, (0, 0), fx=upscale, fy=upscale,
                                 interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            variants = [gray]
            if nvars == 2:
                variants.append(cv2.adaptiveThreshold(
                    gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY, 11, 2))
            best_lines, best = [], -1
            for v in variants:
                lines, conf = read(v)
                score = A._hindi_line_score(lines)
                if score > best:
                    best, best_lines = score, lines
            scores.append(best)
            texts.append(" ".join(best_lines))
        el = time.perf_counter() - t
        rows[key] = (el, scores, texts)
        print(f"  {key:22} {el:7.3f}s  ({el / len(cards):5.3f}s/card)  "
              f"label scores {scores}")

    baseline = rows[f"2x / 2 reads"]
    print(f"\n  candidate 1x/1 read vs today's 2x/2 reads:")
    cand = rows["1x / 1 read"]
    print(f"    speed   {baseline[0] / cand[0]:.2f}x faster")
    better = sum(1 for a, b in zip(cand[2], baseline[2]) if a == b)
    print(f"    identical winning text on {better}/{len(cards)} cards")
    for i, (a, b) in enumerate(zip(cand[2], baseline[2])):
        if a != b:
            print(f"      card {i}: 1x={a[:60]!r}")
            print(f"              2x={b[:60]!r}")
    print()