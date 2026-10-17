"""Is the 14-thread pool actually scaling, and if not, why?

Measured: 30 cards take 97.69s sequential but roughly 43s on 14 threads --
about 2.3x, not 14x.  Two candidate causes, and they need different fixes:

  A. The GIL.  If the dominant call does not release it, threads serialise and
     more workers buy nothing.  A doubling pattern (2, 4, 8...) with near-flat
     per-thread throughput is the signature.
  B. CPU oversubscription.  WSL2 exposes 16 vCPUs; if the host has fewer real
     cores, or if the container sees a different count than the host, threads
     contend in the scheduler.  Throughput rises to a plateau and flattens.

This measures both directly: pure CPU work (no Python-level blocking), the real
`_tesserocr_data` call, and PIL encoding, each at 1/2/4/8/14 threads.
"""
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor

sys.stdout = open("/data/thread_scale.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import os
import fitz
import numpy as np
import cv2
import PIL
import ocr_pdf_api as A

print("=" * 78)
print("ENVIRONMENT")
print("=" * 78)
print(f"  os.cpu_count()            = {os.cpu_count()}")
print(f"  affinity (sched_getaffinity) = "
      f"{len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else 'n/a'}")
print(f"  cgroup cpu.max            = "
      f"{open('/sys/fs/cgroup/cpu.max').read().strip() if os.path.exists('/sys/fs/cgroup/cpu.max') else 'n/a'}")
print(f"  OMP_THREAD_LIMIT          = {os.getenv('OMP_THREAD_LIMIT')}")
print(f"  OCR_TESS_BACKEND          = {os.getenv('OCR_TESS_BACKEND')}")
print(f"  cv2.getNumThreads()       = {cv2.getNumThreads()}")
try:
    print(f"  tesserocr                 = {A.tesserocr.__version__ if hasattr(A, 'tesserocr') else 'n/a'}")
except Exception:
    pass

# ---- build a real card crop -------------------------------------------------
SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
cards = [raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
             int(r.x0 * SCALE):int(r.x1 * SCALE)].copy() for r in rects]
card = cards[0]
print(f"\n  card crop = {card.shape}")

# warm the engine so initialisation is not timed
A._extract_card(page, rects[0], 4, 0)


def timed_pass(fn, n_threads, items):
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(fn, items))
    return time.perf_counter() - t


def pure_cpu(n):
    """Pure arithmetic: no I/O, no C-extension, pure GIL contention test."""
    total = 0
    for i in range(n):
        total += i * i
    return total


def cpu_work(_ignored):
    return pure_cpu(2_000_000)


def pil_encode(_ignored):
    ok, buf = cv2.imencode(".png", card)
    return len(buf)


def tess_data(_ignored):
    """The real hot call -- full TSV layout analysis.

    `_tess_data` takes an image argument, not encoded bytes, so encode first and
    pass the array.  An earlier version passed bytes and raised inside the pool,
    which is why the scaling table printed empty.
    """
    ok, buf = cv2.imencode(".png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))
    if not ok:
        return None
    return A._tess_data(cv2.imdecode(buf, cv2.IMREAD_COLOR),
                        lang="hin+eng", config="--oem 3 --psm 6")


def tess_string(_ignored):
    ok, buf = cv2.imencode(".png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))
    if not ok:
        return None
    return A._tess_string(cv2.imdecode(buf, cv2.IMREAD_COLOR),
                          lang="hin+eng", config="--oem 3 --psm 6")


WORK = 14
print("\n" + "=" * 78)
print("SCALING: 14 identical units of work spread over N threads")
print("=" * 78)
print(f"{'threads':>8} {'pure CPU':>12} {'speedup':>9} "
      f"{'PIL enc':>11} {'speedup':>9} {'tess_data':>12} {'speedup':>9}")
print("-" * 78)

base = {}
for name, fn in (("cpu", cpu_work), ("pil", pil_encode), ("data", tess_data)):
    base[name] = timed_pass(fn, 1, [None] * WORK)
    print(f"  baseline {name}: {base[name]:.3f}s")

print(f"\n{'threads':>8} {'pure CPU':>11} {'x':>7} "
      f"{'PIL enc':>10} {'x':>7} {'tess_data':>11} {'x':>7}")
print("-" * 60)

for n in (1, 2, 4, 7, 14):
    cpu_s = timed_pass(cpu_work, n, [None] * WORK)
    pil_s = timed_pass(pil_encode, n, [None] * WORK)
    dat_s = timed_pass(tess_data, n, [None] * WORK)
    print(f"{n:>8} {cpu_s:>11.3f} {base['cpu'] / cpu_s:>7.2f} "
          f"{pil_s:>10.3f} {base['pil'] / pil_s:>7.2f} "
          f"{dat_s:>11.3f} {base['data'] / dat_s:>7.2f}")

print("-" * 78)
print("baseline 1-thread: "
      f"cpu {base['cpu']:.3f}s  pil {base['pil']:.3f}s  data {base['data']:.3f}s")

# ---- the real question: does the pipeline's own pool scale? -----------------
print("\n" + "=" * 78)
print("REAL PIPELINE POOL -- _extract_card over 14 card positions")
print("=" * 78)
for n in (1, 4, 8, 14):
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(
            lambda r: A._extract_card(page, r, 4, 0), rects[:WORK]))
    el = time.perf_counter() - t
    print(f"  {n:>2} threads: {el:7.2f}s for {WORK} cards  "
          f"({el / WORK * 1000:6.0f} ms/card effective)")

# ---- does one _tesserocr_data release the GIL? -----------------------------
print("\n" + "=" * 78)
print("GIL TEST -- elapsed wall time for 1 call, then 8 concurrent calls")
print("=" * 78)
t = time.perf_counter()
tess_data(None)
one = time.perf_counter() - t

t = time.perf_counter()
with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(tess_data, [None] * 8))
eight = time.perf_counter() - t
print(f"  1 call  : {one:6.3f}s")
print(f"  8 calls : {eight:6.3f}s  (ideal {one * 8:.3f}s, "
      f"perfect parallel {one:.3f}s)")
print(f"  -> if 8 calls take ~8x one call, the GIL is NOT released")
print(f"     parallel efficiency: {one * 8 / eight * 100:.0f}%")