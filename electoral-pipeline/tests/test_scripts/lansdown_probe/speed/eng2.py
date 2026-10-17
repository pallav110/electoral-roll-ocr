"""Isolate the crash: which tesserocr call is dying, and does it die in a thread?

The previous probe stopped right after `first GetUTF8Text (warms)` printed.
This runs each stage in isolation with the exception surfaced, and records whether
`GetUTF8Text` needs `SetSourceResolution` first -- a tesserocr version detail that
silently changes behaviour.
"""
import sys
import time
import traceback

sys.stdout = open("/data/eng2.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
from PIL import Image
from tesserocr import PyTessBaseAPI

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A_rects = None
import ocr_pdf_api as A
rects = A._voter_card_rects(page)
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
r = rects[0]
card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
           int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
img = Image.fromarray(cv2.cvtColor(card, cv2.COLOR_RGB2GRAY))
print(f"card {card.shape} -> PIL {img.size} mode={img.mode}")

def step(name, fn):
    t = time.perf_counter()
    try:
        out = fn()
        print(f"  OK   {name:34} {time.perf_counter() - t:7.4f}s")
        return out
    except Exception as exc:
        print(f"  FAIL {name:34} {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2, file=sys.stdout)
        return None


print("=" * 74)
print("STAGE BY STAGE")
print("=" * 74)
api = step("PyTessBaseAPI(lang=hin+eng)", lambda: PyTessBaseAPI(path="", lang="hin+eng"))
if api is None:
    raise SystemExit(1)

step("SetImage", lambda: api.SetImage(img))
step("GetUTF8Text  (no SetSourceResolution)", lambda: api.GetUTF8Text())
step("GetUTF8Text  (again)", lambda: api.GetUTF8Text())
step("SetSourceResolution(300)", lambda: api.SetSourceResolution(300))
step("SetImage + SetSourceResolution + GetUTF8Text",
     lambda: (api.SetImage(img), api.SetSourceResolution(300), api.GetUTF8Text())[2])
step("GetTSVText(0)", lambda: api.GetTSVText(0))

print("\n" + "=" * 74)
print("WARM CALL COST, measured properly")
print("=" * 74)


def bench(fn, n=5):
    fn()                                   # warm
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n


api.SetImage(img)
api.SetSourceResolution(300)
s = bench(lambda: (api.SetImage(img), api.GetUTF8Text())[1])
print(f"  GetUTF8Text  {s:.4f}s per call")
try:
    api.SetImage(img)
    api.SetSourceResolution(300)
    v = bench(lambda: (api.SetImage(img), api.GetTSVText(0))[1], 3)
    print(f"  GetTSVText   {v:.4f}s per call   ({v / s:.0f}x the string call)")
except Exception as exc:
    print(f"  GetTSVText unavailable: {exc}")

print("\n" + "=" * 74)
print("IN A THREAD POOL -- does the engine survive SetImage/GetUTF8Text?")
print("=" * 74)
from concurrent.futures import ThreadPoolExecutor


def job(_):
    a = PyTessBaseAPI(path="", lang="hin+eng")
    a.SetImage(img)
    a.SetSourceResolution(300)
    return len(a.GetUTF8Text())


for n in (1, 4, 8, 14):
    t = time.perf_counter()
    ok = 0
    with ThreadPoolExecutor(max_workers=n) as pool:
        for res in pool.map(job, range(14)):
            ok += res is not None
    print(f"  {n:>2} threads: {time.perf_counter() - t:7.3f}s for 14 reads "
          f"({ok}/14 succeeded)")