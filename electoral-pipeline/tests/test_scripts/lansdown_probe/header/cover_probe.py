import sys, json, time
sys.stdout = open("/data/cover_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import ocr_pdf_api as A

print("ECI_STATES_URL env:", __import__("os").getenv("ECI_STATES_URL"))
states = A._active_eci_states()
print("_active_eci_states() ->", states)

d = fitz.open("/data/roll107.pdf")
page1 = d[0]

print("\n--- _read_state_token ---")
try:
    print(repr(A._read_state_token(page1))[:300])
except Exception as e:
    print("RAISED:", type(e).__name__, e)

print("\n--- _extract_state_code ---")
try:
    print(repr(A._extract_state_code(page1))[:300])
except Exception as e:
    print("RAISED:", type(e).__name__, e)

print("\n--- _extract_booth_code_from_cover ---")
try:
    print(repr(A._extract_booth_code_from_cover(page1))[:300])
except Exception as e:
    print("RAISED:", type(e).__name__, e)

print("\n--- _extract_anubhag_entries_from_cover ---")
try:
    t0 = time.time()
    print(repr(A._extract_anubhag_entries_from_cover(page1))[:600], f"({time.time()-t0:.1f}s)")
except Exception as e:
    print("RAISED:", type(e).__name__, e)

print("\n--- _extract_roll_header_metadata (the one the pipeline calls) ---")
try:
    t0 = time.time()
    md = A._extract_roll_header_metadata(page1)
    print(f"({time.time()-t0:.1f}s)")
    print(json.dumps(md, ensure_ascii=False, indent=2, default=str)[:1500])
except Exception as e:
    import traceback; traceback.print_exc(file=sys.stdout)

print("\n--- ocr_cover module ---")
try:
    import ocr_cover
    print("ocr_cover attrs:", [a for a in dir(ocr_cover) if not a.startswith("_")][:20])
    if hasattr(ocr_cover, "extract_cover"):
        t0 = time.time()
        print(json.dumps(ocr_cover.extract_cover(page1), ensure_ascii=False, indent=2, default=str)[:1200],
              f"({time.time()-t0:.1f}s)")
except Exception as e:
    import traceback; traceback.print_exc(file=sys.stdout)