#!/usr/bin/env python3
"""Quick runner: extract page 3 from the electoral-roll PDF and write results to page3_results.json."""
import json
import sys
import time
from pathlib import Path

PDF_PATH = Path(r"C:\Users\pallav\Desktop\Python\OCR\input\2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
OUTPUT_PATH = Path(r"C:\Users\pallav\Desktop\Python\OCR\ground_truth_per_page_values_verified\json_files\page3_results.json")

# Import extraction function from the API module (no HTTP server needed)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
from ocr_pdf_api import _extract_pdf_ocr_unlocked

pdf_bytes = PDF_PATH.read_bytes()
print(f"[run] PDF loaded: {len(pdf_bytes):,} bytes", flush=True)

t0 = time.perf_counter()
result = _extract_pdf_ocr_unlocked(
    pdf_bytes=pdf_bytes,
    filename=PDF_PATH.name,
    start_page=3,
    end_page=3,
    whole_pdf=False,
    skip_non_voter_pages=True,
)
elapsed = time.perf_counter() - t0

OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

records = result.get("records", [])
print(f"\n[run] Done in {elapsed:.2f}s - {len(records)} records -> {OUTPUT_PATH}")
if records:
    print("[run] First record sample:")
    print(json.dumps(records[0], ensure_ascii=True, indent=2))
