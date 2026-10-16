import sys, os, time, json
sys.stdout = open("/data/time_meta_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import ocr_pdf_api as A

print("OCR_CARD_WORKERS =", A._OCR_CARD_WORKERS)
print("ECI_STATES_URL =", os.getenv("ECI_STATES_URL"))
print("ECI_SOURCE_VERIFY =", os.getenv("ECI_SOURCE_VERIFY_ENABLED"))

# ---- 1. metadata on the real path ----
print("\n" + "=" * 70)
print("METADATA (real extraction, page 3 only)")
print("=" * 70)
with open("/data/roll107.pdf", "rb") as fh:
    blob = fh.read()
t0 = time.time()
res = A._extract_pdf_ocr_unlocked(blob, "roll107.pdf", 3, 3, False, True)
el = time.time() - t0
print(f"\nextract of page 3 took {el:.1f}s with {A._OCR_CARD_WORKERS} workers")
print(f"records: {len(res.get('records', []))}")
print("\ndocument_metadata:")
print(json.dumps(res.get("document_metadata", {}), ensure_ascii=False, indent=2))

# ---- 2. what the header reader produced ----
import fitz
d = fitz.open("/data/roll107.pdf")
page = d[2]
print("\nraw header text via pdftotext-equivalent (page.get_text on cover):")
for pn in (0, 1, 2):
    t = d[pn].get_text("text").strip()
    print(f"  page {pn+1}: {len(t)} chars -> {t[:80]!r}")

# ---- 3. state lookup dependency ----
print("\n" + "=" * 70)
print("STATE LOOKUP")
print("=" * 70)
try:
    states = A._active_eci_states()
    print("_active_eci_states() ->", type(states).__name__,
          f"len={len(states) if hasattr(states,'__len__') else '?'}")
except Exception as e:
    print("_active_eci_states() RAISED:", type(e).__name__, e)