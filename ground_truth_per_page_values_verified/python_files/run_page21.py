#!/usr/bin/env python3
"""Quick runner: extract page 21 from the electoral-roll PDF."""
import json, sys, time
from pathlib import Path

PDF_PATH = Path(r"C:\Users\pallav\Desktop\Python\OCR\input\2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "json_files" / "page21_results.json"

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
from ocr_pdf_api import _extract_pdf_ocr_unlocked

pdf_bytes = PDF_PATH.read_bytes()
print(f"[run] PDF loaded: {len(pdf_bytes):,} bytes", flush=True)

t0 = time.perf_counter()
result = _extract_pdf_ocr_unlocked(
    pdf_bytes=pdf_bytes, filename=PDF_PATH.name,
    start_page=21, end_page=21, whole_pdf=False, skip_non_voter_pages=True,
)
elapsed = time.perf_counter() - t0

OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
records = result.get("records", [])
print(f"[run] Done in {elapsed:.2f}s - {len(records)} records -> {OUTPUT_PATH}")
