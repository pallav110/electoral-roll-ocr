"""Trace HIN-100 section-code reads without exporting voter fields."""
import sys
sys.stdout = open("/data/hin100_code.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz
import ocr_pdf_api as A

path = "/data/pdfs/2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-100-WI.pdf"
doc = fitz.open(path)
print(f"pages={len(doc)}")
for page_index in (2, 3, min(10, len(doc)-1)):
    page = doc[page_index]
    print("=" * 72)
    print(f"page={page_index+1}")
    located = A._find_anubhag_header_band(page)
    print("located:", bool(located))
    if located:
        rect, text = located
        print("rect:", rect)
        print("hindi_band:", repr(text))
        print("split:", repr(A._split_anubhag_label(text)))
        print("hindi_code:", repr(A._anubhag_code_from_header_text(A._split_anubhag_label(text))))
        print("english_code:", repr(A._anubhag_code_from_english_band(page, rect)))
    print("fallback_code:", repr(A._extract_anubhag_code_from_header_fallback(page)))
    print("fallback_name:", repr(A._extract_anubhag_name_from_header_fallback(page)))
print("metadata:", {k:v for k,v in A._extract_roll_header_metadata(doc[2]).items()
                         if k in ('ac_code','booth_code','anubhag_code','anubhag_name','anubhag_review_reason')})
