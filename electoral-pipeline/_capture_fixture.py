"""Capture real Tesseract output for the Hindi parser.

Run inside the ocr container. Records the exact line list that
parse_voter_box_from_ocr_lines() receives, for real cards in the real roll,
so the parser can be regression-tested against OCR output rather than
against the PDF's embedded text layer (which contains almost no merged
name+relation lines and would hide the failure this fixture exists to catch).
"""
import json
import sys

sys.path.insert(0, "/app")

import fitz

import ocr_pdf_api as O

PDF = "/data/pdfs/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
OUT = "/data/ocr_fixture_cards.json"
PAGES = [2, 3, 5]  # 0-indexed -> printed pages 3, 4, 6

captured = []
orig = O.parse_voter_box_from_ocr_lines


def spy(lines, *a, **k):
    captured.append([ln for ln in (lines or []) if ln and ln.strip()])
    return orig(lines, *a, **k)


O.parse_voter_box_from_ocr_lines = spy

with fitz.open(PDF) as doc:
    for pi in PAGES:
        page = doc[pi]
        rects = O._voter_card_rects(page)
        for i in range(len(rects)):
            captured.clear()
            try:
                rec = O._extract_card(page, rects[i], page_number=pi + 1, card_index=i)
            except Exception:
                continue
            if not captured:
                continue
            # _extract_card returns None for a blank grid slot; those are not
            # parse failures and carry no lines worth recording.
            if rec is None:
                captured.clear()
                continue
            captured.append(
                {
                    "page": pi + 1,
                    "card": i,
                    "lines": captured[0],
                    "record": {
                        k: (rec.get(k) or "")
                        for k in (
                            "voter_first_name",
                            "voter_middle_name",
                            "voter_sur_name",
                            "relation_name",
                            "voter_husband_name",
                            "voter_father_name",
                            "voter_mother_name",
                            "voter_other_name",
                            "house_no",
                            "age",
                            "gender",
                        )
                    },
                }
            )

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(captured, fh, ensure_ascii=False, indent=1)

print("captured cards:", len(captured))
print("written:", OUT)
