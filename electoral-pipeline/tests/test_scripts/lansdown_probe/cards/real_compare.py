"""Compare shadow occupancy with the real fixed extractor on one sparse page.

The occupancy classifier has already run on real Lansdown PDFs. This probe now
runs the existing fixed-grid card extractor on HIN-053 page 11 and compares
record presence to the predicted occupied/blank slots. It does not modify the
pipeline or database.
"""
import sys
import time

sys.stdout = open("/data/real_compare.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import numpy as np
import ocr_pdf_api as A

SCALE = 200 / 72.0
path = "/data/roll53.pdf"
page_index = 10

doc = fitz.open(path)
page = doc[page_index]
pix = page.get_pixmap(dpi=200, alpha=False)
raw = np.frombuffer(pix.samples, np.uint8).reshape(
    pix.height, pix.width, pix.n)[:, :, :3]
rects = A._voter_card_rects(page)
states = []
records = []
started = time.perf_counter()
for i, r in enumerate(rects):
    crop = raw[int(r.y0 * SCALE):int(r.y1 * SCALE),
               int(r.x0 * SCALE):int(r.x1 * SCALE)].copy()
    states.append(A._classify_card_occupancy(crop))
    records.append(A._extract_card(page, r, page_index + 1, i))

report = A._occupancy_shadow_report(states, records, fixed_skipped=False)
print(f"page={page_index + 1} slots={len(rects)} elapsed={time.perf_counter()-started:.2f}s")
print(f"occupancy: occupied={report['predicted_occupied']} blank={report['predicted_blank']} uncertain={report['predicted_uncertain']}")
print(f"fixed extractor records={sum(r is not None for r in records)}")
print(f"record presence disagreements={report['record_presence_disagreements']}")
for i, (state, record) in enumerate(zip(states, records)):
    if state == A.CARD_OCCUPANCY_BLANK or record is not None:
        print(f"slot={i:02d} occupancy={state:9} record={'yes' if record else 'no'}")
doc.close()
