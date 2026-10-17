"""Where does 50.9s/page actually go?

Known so far, from separate probes:
  Tesseract, ONE full-card Hindi read, 30 cards serial : 69.31s  -> ~5s on 14
  Paddle stacked pass, 30 cards                        :  4.34s
  Paddle deleted vote, 30 cards                        :  3.56s
                                                            --------
  accounted                                               ~13s of 50.9s

So ~38s is unaccounted.  The likely cause is that a card is not read once: the
pipeline runs ~28 Tesseract reads per card, and the focused re-reads crop small
regions OF A 689x1761px LANSDOWN CARD, so even a small crop carries a large
absolute pixel count.

This profiles the real extraction path with cProfile, which attributes time to
the actual functions called, rather than to my model of what is called.
"""
import sys
import cProfile
import pstats
import io as _io

sys.stdout = open("/data/cprof.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import time
import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
PAGES = "/data/roll107.pdf"


def run_one_page(page):
    """Reproduce the page body: render, crop, extract every card."""
    pix = page.get_pixmap(dpi=200, alpha=False)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    rects = A._voter_card_rects(page)
    cards = [
        raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
            int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
        for r in rects
    ]
    return [A._extract_card(c) for c in cards]


doc = fitz.open(PAGES)
page = doc[3]                       # printed page 4

# Warm the Paddle model and the Tesseract engines so the profile measures work,
# not one-time initialisation.
t = time.perf_counter()
A._get_paddle()
print(f"paddle init: {time.perf_counter() - t:.2f}s")

# Sequential baseline: what one card costs when nothing else runs.
one = run_one_page(page)[:3]
print(f"3 cards sequential: see profile below\n")

profiler = cProfile.Profile()
profiler.enable()
records = run_one_page(page)
profiler.disable()

buf = _io.StringIO()
stats = pstats.Stats(profiler, stream=buf)
stats.sort_stats("cumulative")
print("=" * 78)
print("cProfile of ONE real page (30 cards), cumulative")
print("=" * 78)
print(buf.getvalue()[:6000])

# The same profile, ranked by self time, which is where the time actually sits.
buf2 = _io.StringIO()
stats2 = pstats.Stats(profiler, stream=buf2)
stats2.sort_stats("tottime")
print("=" * 78)
print("cProfile of ONE real page (30 cards), by SELF time")
print("=" * 78)
print(buf2.getvalue()[:4500])

print(f"records produced: {sum(1 for r in records if r)}/{len(records)}")