"""Find the fix for Tesseract thread scaling, by measurement.

Measured: tess_data gets SLOWER with threads (0.5x at 14), tess_string 0.3x.
Two candidate causes, and the fixes are different:

  A. The GIL.  tesserocr's GetUTF8Text holds the GIL.  Fix: stop using threads
     for Tesseract -- use PROCESSES, which have no shared GIL and no shared
     engine.
  B. Per-thread engine construction.  `_tesserocr_data` is documented as
     thread-local, so N threads build N engines, each loading hin+eng.  Fix:
     measure the cost of engine construction; if it dominates, that is a
     different problem entirely.

This measures, per-thread engine build cost, and compares thread pool against
process pool for the real workload.  The answer determines which fix to apply.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

sys.stdout = open("/data/engine.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
import ocr_pdf_api as A

SCALE = 200 / 72.0
WORK = 14

doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]


def card_array(index):
    r = rects[index % len(rects)]
    return raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()


print("=" * 74)
print("1. HOW EXPENSIVE IS ENGINE CONSTRUCTION?")
print("=" * 74)

t = time.perf_counter()
from tesserocr import PyTessBaseAPI
api = PyTessBaseAPI(path="", lang="hin+eng")
print(f"  first engine build (loads model): {time.perf_counter() - t:7.3f}s")
t = time.perf_counter()
text = api.GetUTF8Text()
print(f"  first GetUTF8Text (warms):       {time.perf_counter() - t:7.3f}s  "
      f"({len(text)} chars)")

# What does the module's thread-local builder cost on a warm engine?
t = time.perf_counter()
api2 = A._tesserocr_engine_for_thread() if hasattr(A, "_tesserocr_engine_for_thread") \
    else PyTessBaseAPI(path="", lang="hin+eng")
print(f"  module's engine builder:         {time.perf_counter() - t:7.3f}s")

# Repeated Text() on one warm engine: the cost per read once warm.
img = Image.fromarray(cv2.cvtColor(card_array(0), cv2.COLOR_RGB2BGR))
api.SetImage(img)
api.SetSourceResolution(300)
t = time.perf_counter()
for _ in range(5):
    api.SetImage(img)
    api.GetUTF8Text()
warm_each = (time.perf_counter() - t) / 5
print(f"  warm GetUTF8Text, per call:      {warm_each:7.4f}s")

tsv = None
api.SetImage(img)
t = time.perf_counter()
for _ in range(3):
    api.SetImage(img)
    try:
        api.GetTSVText(0)
    except Exception as exc:
        tsv = f"unavailable: {exc}"
        break
if tsv is None:
    warm_tsv = (time.perf_counter() - t) / 3
    print(f"  warm GetTSVText, per call:       {warm_tsv:7.4f}s  "
          f"({warm_tsv / warm_each:.0f}x the string call)")
else:
    print(f"  GetTSVText -> {tsv}")
del api, api2


# ---- the decisive comparison: threads vs processes --------------------------
print("\n" + "=" * 74)
print("2. THREADS vs PROCESSES for the same Tesseract workload")
print("=" * 74)


def _work(payload):
    """Runs in a thread OR a process; module-level so processes can pickle it."""
    img_bytes, lang, config = payload
    api = _PROC_API.get(lang) if _PROC_API is not None else None
    if api is None:
        from tesserocr import PyTessBaseAPI
        api = PyTessBaseAPI(path="", lang=lang)
        _PROC_API[lang] = api
    api.SetImage(Image.open(img_bytes))
    api.SetSourceResolution(300)
    return api.GetUTF8Text()


_PROC_API = {}
_CARD_PNG = b""
_LANG = "hin+eng"
_CONFIG = "--oem 3 --psm 6"


def thread_job(_):
    from tesserocr import PyTessBaseAPI
    api = PyTessBaseAPI(path="", lang=_LANG)
    api.SetImage(Image.open(_CARD_PNG))
    api.SetSourceResolution(300)
    return api.GetUTF8Text()


def proc_job(_):
    return _work((_CARD_PNG, _LANG, _CONFIG))


ok, enc = cv2.imencode(".png", cv2.cvtColor(card_array(0), cv2.COLOR_RGB2BGR))
import io
_CARD_PNG = enc.tobytes()

# single-process baseline
t = time.perf_counter()
proc_job(0)
one_call = time.perf_counter() - t
print(f"  1 read (engine built): {one_call:7.3f}s")
print(f"  1 read (engine warm) : {warm_each:7.3f}s")
print(f"  -> engine construction is {one_call - warm_each:.3f}s of that")

print(f"\n  {WORK} reads, wall clock:")
print(f"  {'threads':>8} {'elapsed':>10}")
for n in (1, 2, 4, 8, 14):
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(thread_job, range(WORK)))
    el = time.perf_counter() - t
    print(f"  {n:>8} {el:>10.3f}s   ({one_call * WORK / el:>5.1f}x ideal)")

print()
for n in (2, 4, 7, 14):
    t = time.perf_counter()
    with ProcessPoolExecutor(max_workers=n) as pool:
        list(pool.map(proc_job, range(WORK)))
    el = time.perf_counter() - t
    print(f"  processes n={n:<3} {el:>10.3f}s   ({one_call * WORK / el:>5.1f}x ideal)")