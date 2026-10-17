"""Why does card 0 read age 88 when the card prints 41?

`88` is not a misread digit -- 41 and 88 share nothing. That pattern says the
reader is looking at the WRONG PIXELS rather than misrecognising the right
ones, so this traces which region the age comes from and renders each.

The card (Lansdown, page 4, card 0) prints:
    नाम : गीता देवी
    पति का नाम: नरेंद्र सिंह
    मकान संख्या : 7
    आयु : 41 लिंग : महिला

and returns age='88'.
"""
import sys

sys.stdout = open("/data/age_probe.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import cv2
import ocr_pdf_api as A

SCALE = 200 / 72.0
doc = fitz.open("/data/roll107.pdf")
page = doc[3]
rects = A._voter_card_rects(page)
r = rects[0]
card = raw = None
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
card = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
           int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()

h, w = card.shape[:2]
print(f"card {card.shape}")

print("\nREGIONS (fractions of card height):")
for nm, reg in (("AGE_REGION", A.AGE_REGION),
                ("GENDER_AGE_REGION", A.GENDER_AGE_REGION),
                ("HOUSE_REGION", A.HOUSE_REGION)):
    print(f"  {nm:20} {reg}  -> rows {int(h*reg[1])}..{int(h*reg[3])}, "
          f"cols {int(w*reg[0])}..{int(w*reg[2])}")

# Draw every candidate region so they can be seen against the printed text.
box = card.copy()
for reg, colour in ((A.AGE_REGION, (255, 0, 0)),
                    (A.GENDER_AGE_REGION, (0, 128, 255)),
                    (A.HOUSE_REGION, (0, 200, 0))):
    cv2.rectangle(box, (int(w * reg[0]), int(h * reg[1])),
                  (int(w * reg[2]), int(h * reg[3])), colour, 4)
cv2.imwrite("/data/ap_regions.png", cv2.cvtColor(box, cv2.COLOR_RGB2BGR))
print("\nwrote /data/ap_regions.png "
      "(red=AGE_REGION  orange=GENDER_AGE  green=HOUSE)")

clean = A._remove_outer_card_lines(card)
if clean is None or getattr(clean, "size", 0) == 0:
    clean = card

print("\nWHAT EACH AGE READER RETURNS:")
print(f"  _extract_age_from_clean_card   = "
      f"{A._extract_age_from_clean_card(clean)!r}")

# The deferred paddle age crop.  `_crop_png` is a closure INSIDE _extract_card,
# not a module-level function, so the crop is taken with the public helper.
age_crop = A._crop_card_region(clean, A.AGE_REGION, "AGE_DEFER")
if age_crop is not None and getattr(age_crop, "size", 0):
    print(f"  AGE_DEFER crop shape            = {age_crop.shape}")
    big = cv2.resize(age_crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    cv2.imwrite("/data/ap_age_crop.png", cv2.cvtColor(big, cv2.COLOR_BGR2GRAY))
else:
    print("  AGE_DEFER crop empty")

ga = A._crop_card_region(clean, A.GENDER_AGE_REGION, "GENDER_AGE")
if ga is not None and getattr(ga, "size", 0):
    big = cv2.resize(ga, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    cv2.imwrite("/data/ap_gender_age.png", big)
    print(f"  GENDER_AGE crop shape           = {ga.shape}")
    cv2.imwrite("/data/ap_gender_age.png", big)

rec = A._extract_card(page, r, 4, 0)
print(f"\n_extract_card age               = {rec.get('age')!r}")
print(f"_extract_card gender             = {rec.get('gender')!r}")
print(f"needs_review                    = "
      f"{[k for k in rec if 'review' in k.lower()]}")
print(f"full record keys                = "
      f"{ {k: v for k, v in rec.items() if k in ('age','gender','house_no','voter_first_name','id_card_no','is_deleted')} }")

print("\nAGE_LABEL_RE:", A.AGE_LABEL_RE.pattern)
print("AGE_DIGIT_CONFIGS:", A.AGE_DIGIT_CONFIGS)