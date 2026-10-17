"""Thread scaling, minimal and correct.

Measured so far: 30 cards take 97.69s sequential and ~43s on 14 threads -- 2.3x,
not 14x.  cProfile puts 80% of that in `_tesserocr_data`, 52 calls for 8 cards
(6.5 per card) at 0.405s each.

Two candidate causes need separating:
  A. the GIL -- if `_tesserocr_data` holds it, threads serialise;
  B. oversubscription -- 16 vCPUs for fewer real cores, or nested thread pools.

Each function is timed at 1/2/4/8/14 threads over an identical workload, so the
speedup column isolates the cause.  Exceptions are caught per-call, because a
raise inside a pool.map silently truncates the table rather than reporting.
"""
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor

sys.stdout = open("/data/scale3.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

WORK = 14

print("=" * 74)
print("ENVIRONMENT")
print("=" * 74)
print(f"  cpu_count={os.cpu_count()}  "
      f"affinity={len(os.sched_getaffinity(0))}  "
      f"OMP_THREAD_LIMIT={os.getenv('OMP_THREAD_LIMIT')}  "
      f"backend={os.getenv('OCR_TESS_BACKEND')}")
print(f"  cv2.getNumThreads()={cv2.getNumThreads()}   "
      f"tesserocr={getattr(A.tesserocr, '__version__', 'n/a')}")

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
card = raw[int(rects[0].y0 * SCALE):int(rects[0].y1 * SCALE),
           int(rects[0].x0 * SCALE):int(rects[0].x1 * SCALE)].copy()
gray = cv2.cvtColor(card, cv2.COLOR_RGB2BRAY) if False else cv2.cvtColor(card, cv2.COLOR_RGB2GRAY)
print(f"  card={card.shape}")

# Warm both engines outside the measurements.
A._get_paddle()
try:
    A._tess_data(gray, lang="hin+eng", config="--oem 3 --psm 6")
except Exception as exc:
    print(f"  WARNING warm-up _tess_data failed: {type(exc).__name__}: {exc}")

errors = []


def cpu_work(_):
    total = 0
    for i in range(1_500_000):
        total += i * i
    return total


def pil_encode(_):
    try:
        ok, buf = cv2.imencode(".png", card)
        return len(buf)
    except Exception as exc:
        errors.append(f"pil: {exc}")
        return 0


def tess_data(_):
    try:
        return A._tess_data(gray, lang="hin+eng", config="--oem 3 --psm 6")
    except Exception as exc:
        errors.append(f"data: {type(exc).__name__}: {exc}")
        return None


def tess_string(_):
    try:
        return A._tess_string(gray, lang="hin+eng", config="--oem 3 --psm 6")
    except Exception as exc:
        errors.append(f"string: {type(exc).__name__}: {exc}")
        return None


def timeit(fn, threads):
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(fn, range(WORK)))
    return time.perf_counter() - t, results


CASES = (("pure CPU", cpu_work), ("PIL encode", pil_encode),
         ("tess_data", tess_data), ("tess_string", tess_string))

base = {}
for name, fn in CASES:
    el, _res = timeit(fn, 1)
    base[name] = el
    print(f"  1-thread baseline {name:12} {el:7.3f}s")

print("\n" + "=" * 74)
print(f"14 identical units over N threads  (speedup vs 1 thread)")
print("=" * 74)
header = f"{'threads':>8}"
for name, _fn in CASES:
    header += f"{name:>15}"
print(header)
print("-" * len(header))
for n in (1, 2, 4, 8, 14):
    line = f"{n:>8}"
    for name, fn in CASES:
        el, _res = timeit(fn, n)
        line += f"{el:>9.3f}({base[name] / el:>4.1f}x)"
    print(line)

if errors:
    print("\n  distinct errors:")
    for e in sorted(set(errors))[:5]:
        print(f"    {e}")