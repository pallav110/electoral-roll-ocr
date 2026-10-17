"""Where does the page time actually go?  Profile the real call signature.

`_extract_card(page, rect, page_number, card_index)` takes the PAGE and the card
rect, not a pre-sliced array -- so the earlier probe called it with an array and
died silently after Paddle init.  This uses the real signature and profiles the
whole page body, so time is attributed to the functions actually called.

Sequential on purpose: cProfile measures one thread, and the pipeline's
parallelism is a ThreadPoolExecutor above this level.  Dividing the sequential
total by the worker count gives the parallel-equivalent cost, which is how the
per-page figure should be compared against the 7-9s target.
"""
import sys
import time
import cProfile
import pstats
import io as _io

sys.stdout = open("/data/cprof2.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import ocr_pdf_api as A

CASES = (("S28-40 Lansdown", "/data/roll107.pdf", 3),
         ("S24-53", "/data/roll_s24_300.pdf", 3))

for label, path, page_no in CASES:
    doc = fitz.open(path)
    page = doc[page_no]
    rects = A._voter_card_rects(page)
    print("=" * 78)
    print(f"{label}   page {page_no + 1}   {len(rects)} card positions")
    print("=" * 78)

    # Warm the engines so initialisation is not profiled as work.
    A._get_paddle()
    warm = A._extract_card(page, rects[0], page_no + 1, 0)
    print(f"  warm-up card produced: {warm is not None}")

    t = time.perf_counter()
    single = [A._extract_card(page, r, page_no + 1, i)
              for i, r in enumerate(rects)]
    sequential = time.perf_counter() - t
    workers = 14
    print(f"  30 cards sequential      : {sequential:7.2f}s")
    print(f"  same work on {workers:>2} threads : "
          f"{sequential / workers:7.2f}s  (ideal, no contention)")

    # Now profile a smaller slice so the output is readable, and attribute it.
    prof = cProfile.Profile()
    prof.enable()
    for r in rects[:8]:
        A._extract_card(page, r, page_no + 1, 0)
    prof.disable()

    buf = _io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(22)
    print()
    print(buf.getvalue()[:5200])
    doc.close()
    print()