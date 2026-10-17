"""Tesseract is no longer the bottleneck.  What is?

After removing the 2x upscale, 30 cards take 24.95s sequentially, of which
cProfile attributes 3.2s to Tesseract.  The page still takes ~27s wall, and the
card pool accounts for only ~1.8s of that.  So roughly 25s is spent OUTSIDE the
card Tesseract reads -- i.e. in the sequential Paddle pass and whatever else
runs after the pool drains.

This times the pipeline's own phases in the order it runs them: grid probe, the
card pool, then the Paddle pass, using the real function calls rather than a
reconstruction of them.
"""
import sys
import time

sys.stdout = open("/data/phases.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
PATH, PAGE_NO = "/data/roll107.pdf", 3

doc = fitz.open(PATH)
page = doc[PAGE_NO]
W, H = page.rect.width, page.rect.height

t = time.perf_counter()
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
print(f"render 200 DPI            {time.perf_counter() - t:7.2f}s")

t = time.perf_counter()
looks, hits, count = A._page_looks_like_voter_grid(page)
print(f"grid probe ({count} cards)  {time.perf_counter() - t:7.2f}s  "
      f"looks_like_grid={looks} hits={hits}")

rects = A._voter_card_rects(page)
cards = [raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
             int(r.x0 * SCALE):int(r.x1 * SCALE)].copy() for r in rects]

A._get_paddle()
paddle = A._get_paddle()


def timed(name, fn, items):
    """Time `fn` over `items`; `items` is a LIST, so a single ndarray is
    wrapped -- passing an ndarray as *args iterates its rows instead."""
    t = time.perf_counter()
    out = [fn(i) for i in items]
    el = time.perf_counter() - t
    print(f"{name:26} {el:7.2f}s  ({el / max(1, len(items)):6.3f}s each)",
          flush=True)
    return el, out


# --- the sequential Paddle pass, stage by stage, as _extract_card does it ---
def removed(card):
    c = A._remove_outer_card_lines(card)
    return c if c is not None and getattr(c, "size", 0) else card


timed("remove_outer_card_lines", removed, cards)

crops = [removed(c) for c in cards]

# The house-number English line reader: (0.12,0.46,0.99,0.64) on every card.
house_crops = []
for c in crops:
    cr = A._crop_card_region(c, (0.12, 0.46, 0.99, 0.64), "HOUSE_ENGLISH")
    house_crops.append(cr)
print(f"\nhouse English crops built: "
      f"{sum(1 for c in house_crops if c is not None and getattr(c,'size',0))}"
      f"/{len(crops)}")


def house_read(cr):
    if cr is None or getattr(cr, "size", 0) == 0:
        return ""
    # `_tess_string` goes through tesserocr, whose SetImage needs a PIL Image --
    # a raw ndarray has no `.mode`.  Wrap it, or the probe dies before timing.
    from PIL import Image
    return A._tess_string(Image.fromarray(cv2.cvtColor(cr, cv2.COLOR_BGR2GRAY)),
                         lang="eng", config="--oem 3 --psm 7")


timed("tess house English line", house_read, house_crops)

# The stacked Paddle pass, rebuilt exactly.
def stacked(clean):
    crops_info = []
    ser = A._crop_card_region(clean, A.TESSERACT_CARD_CONTENT_REGIONS[0], "SERIAL")
    if ser is not None and getattr(ser, "size", 0):
        s = min(6, max(2, 80 // max(1, ser.shape[0])))
        crops_info.append(("serial", cv2.resize(ser, None, fx=s, fy=s,
                                                interpolation=cv2.INTER_CUBIC)))
    hv = A._crop_card_region(clean, A.HOUSE_REGION, "HOUSE")
    if hv is not None and getattr(hv, "size", 0):
        s = min(6, max(2, 80 // max(1, hv.shape[0])))
        crops_info.append(("house", cv2.resize(hv, None, fx=s, fy=s,
                                               interpolation=cv2.INTER_CUBIC)))
    if not crops_info:
        return None
    max_w = max(c.shape[1] for _, c in crops_info)
    padded = []
    for _f, c in crops_info:
        padded.append(cv2.copyMakeBorder(
            c, 4, 4, 4, max_w - c.shape[1] + 4, cv2.BORDER_CONSTANT,
            value=(255, 255, 255)))
    stack = np.vstack(padded) if len(padded) > 1 else padded[0]
    try:
        paddle.ocr(stack, cls=False)
    except Exception:
        return None
    return stack


timed("paddle stacked pass", stacked, crops)

# The deleted-mark vote, 4 readers per card as measured.
def vote(card):
    ser = A._crop_card_region(card, A.TESSERACT_CARD_CONTENT_REGIONS[0], "SERIAL")
    if ser is None or getattr(ser, "size", 0) == 0:
        return False
    return A._serial_deleted_vote(ser)


timed("paddle deleted vote", vote, cards)

# The full serial Tesseract reader (3 scale targets).
def serial(card):
    ok, buf = cv2.imencode(".png", cv2.cvtColor(card, cv2.COLOR_RGB2BGR))
    return A._extract_serial_with_tesseract(buf.tobytes())


timed("serial tesseract reader", serial, cards[:10])

print("\n" + "=" * 70)
print("If these sequential figures sum to ~25s, the sequential Paddle pass")
print("is the whole remaining cost, and it cannot use the card pool by design.")
print("=" * 70)