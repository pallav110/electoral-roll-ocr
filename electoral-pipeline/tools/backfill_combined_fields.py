"""Combined backfill: re-reads cards with any blank critical field.

Fields repaired: name_hi, name_en, relative_name_hi, relative_name_en,
                 gender_hi, gender_en, age.

Guards: only writes to fields that are currently NULL/empty. Never overwrites
existing values. Groups by (document_id, source_path, page_number) to render
each page at most once.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import os
import sys, re

import cv2
import fitz
import numpy as np
import psycopg

sys.path.insert(0, os.environ.get("OCR_APP_DIR", "/app"))
import ocr_pdf_api as ocr

DATABASE_URL = "postgresql://electoral:electoral@db:5432/electoral"

# The three printed values, and the English label each is stored under. Kept as
# an explicit table rather than a ternary: a `else "Female"` silently filed
# तृतीय लिंग as Female, which is the exact bias this vocabulary exists to fix.
GENDER_EN = {"पुरुष": "Male", "महिला": "Female", "तृतीय लिंग": "Third Gender"}


def _hi(s):
    """True if string contains Devanagari."""
    return bool(s and re.search(r"[ऀ-ॿ]", s))

def _en(s):
    """True if string contains ASCII letters."""
    return bool(s and re.search(r"[A-Za-z]", s))


def fields_for_card(card_image: np.ndarray) -> dict:
    """Re-read all fields from a card. Returns only values we can trust."""
    clean = ocr._remove_outer_card_lines(card_image)
    band = ocr._crop_card_region(clean, ocr.GENDER_AGE_REGION, "GENDER_AGE")

    # Full card OCR for name + relation
    result = {}
    try:
        import cv2 as _cv2
        ok, enc = _cv2.imencode(".png", clean)
        if ok:
            lines, _ = ocr._extract_text_with_tesseract(enc.tobytes())
            parsed = ocr.parse_voter_box_from_ocr_lines(lines or [])
            if parsed and not parsed.get("empty"):
                # Build full name
                name_parts = " ".join(filter(None, [
                    parsed.get("voter_first_name", ""),
                    parsed.get("voter_middle_name", ""),
                    parsed.get("voter_sur_name", ""),
                ])).strip()
                if name_parts and _hi(name_parts):
                    result["name_hi"] = name_parts
                elif name_parts and _en(name_parts):
                    result["name_en"] = name_parts

                # Relation name
                for field in ("voter_father_name", "voter_husband_name",
                              "voter_mother_name", "voter_other_name"):
                    val = parsed.get(field, "").strip()
                    if val:
                        if _hi(val):
                            result["relative_name_hi"] = val
                        elif _en(val):
                            result["relative_name_en"] = val
                        break

                # Age from parser
                age_str = str(parsed.get("age", "") or "").strip()
                if age_str and age_str.isdigit() and ocr.AGE_MIN <= int(age_str) <= ocr.AGE_MAX:
                    result["age"] = int(age_str)

                # Gender from parser
                g = str(parsed.get("gender", "") or "").strip()
                if g in GENDER_EN:
                    result["gender_hi"] = g
                    result["gender_en"] = GENDER_EN[g]
    except Exception:
        pass

    # Focused age reader (stronger than parser)
    if "age" not in result:
        age_str = ocr._extract_age_from_clean_card(clean)
        if age_str and age_str.isdigit() and ocr.AGE_MIN <= int(age_str) <= ocr.AGE_MAX:
            result["age"] = int(age_str)

    # Focused gender reader (stronger than parser)
    if "gender_hi" not in result:
        g = ocr._extract_gender_from_clean_card(clean)
        if g in GENDER_EN:
            result["gender_hi"] = g
            result["gender_en"] = GENDER_EN[g]

    return result


def main():
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT r.id, r.document_id, d.source_path, r.page_number, r.card_index,
                   r.name_hi, r.name_en, r.relative_name_hi, r.relative_name_en,
                   r.gender_hi, r.gender_en, r.age
            FROM electoral_records r JOIN documents d ON d.id = r.document_id
            WHERE r.is_deleted IS NOT TRUE
              AND r.epic_number ~ '^[A-Z]{3}[0-9]{7}$'
              AND (
                r.name_hi IS NULL OR btrim(r.name_hi) = ''
                OR r.relative_name_hi IS NULL OR btrim(r.relative_name_hi) = ''
                OR r.gender_hi IS NULL OR btrim(r.gender_hi) = ''
                OR r.age IS NULL
              )
            ORDER BY d.source_path, r.page_number, r.card_index
        """)
        rows = cur.fetchall()
        print(f"candidates={len(rows)}", flush=True)

        # Group by page
        grouped = defaultdict(list)
        for row in rows:
            row_id, doc_id, source_path, page_number, card_index = row[:5]
            grouped[(doc_id, source_path, page_number)].append(row)

        repaired = 0
        skipped = 0
        for (doc_id, source_path, page_number), page_rows in grouped.items():
            try:
                pdf = fitz.open(source_path)
                page = pdf[page_number - 1]
                pix = page.get_pixmap(dpi=200, alpha=False)
                image = np.frombuffer(pix.samples, np.uint8).reshape(
                    pix.height, pix.width, pix.n)
                page_w, page_h = page.rect.width, page.rect.height
                cards = ocr._voter_card_rects(page)

                work = []
                for row in page_rows:
                    row_id, doc_id2, sp, pg, ci = row[:5]
                    slot = ci - 1
                    if not 0 <= slot < len(cards):
                        skipped += 1
                        continue
                    rect = cards[slot]
                    x0 = max(0, int(rect.x0 / page_w * pix.width))
                    y0 = max(0, int(rect.y0 / page_h * pix.height))
                    x1 = min(pix.width, int(rect.x1 / page_w * pix.width))
                    y1 = min(pix.height, int(rect.y1 / page_h * pix.height))
                    work.append((row, image[y0:y1, x0:x1].copy()))

                with ThreadPoolExecutor(max_workers=16) as pool:
                    results = list(pool.map(
                        lambda item: (item[0], fields_for_card(item[1])), work
                    ))

                for db_row, fields in results:
                    if not fields:
                        continue
                    row_id = db_row[0]
                    (cur_name_hi, cur_name_en, cur_rel_hi, cur_rel_en,
                     cur_gender_hi, cur_gender_en, cur_age) = db_row[5:]

                    sets = []
                    vals = []

                    if (not cur_name_hi or not cur_name_hi.strip()) and fields.get("name_hi"):
                        sets.append("name_hi = %s")
                        vals.append(fields["name_hi"])
                    if (not cur_name_en or not cur_name_en.strip()) and fields.get("name_en"):
                        sets.append("name_en = %s")
                        vals.append(fields["name_en"])
                    if (not cur_rel_hi or not cur_rel_hi.strip()) and fields.get("relative_name_hi"):
                        sets.append("relative_name_hi = %s")
                        vals.append(fields["relative_name_hi"])
                    if (not cur_rel_en or not cur_rel_en.strip()) and fields.get("relative_name_en"):
                        sets.append("relative_name_en = %s")
                        vals.append(fields["relative_name_en"])
                    if (not cur_gender_hi or not cur_gender_hi.strip()) and fields.get("gender_hi"):
                        sets.append("gender_hi = %s")
                        vals.append(fields["gender_hi"])
                        sets.append("gender_en = %s")
                        vals.append(fields.get("gender_en", ""))
                    if cur_age is None and fields.get("age"):
                        sets.append("age = %s")
                        vals.append(fields["age"])

                    if sets:
                        vals.append(row_id)
                        cur.execute(
                            f"UPDATE electoral_records SET {', '.join(sets)} WHERE id = %s",
                            vals
                        )
                        repaired += 1

                conn.commit()
                pdf.close()
            except Exception as exc:
                print(f"page_failed source={source_path} page={page_number}: {exc}", flush=True)

            if repaired and repaired % 200 == 0:
                print(f"repaired={repaired}", flush=True)

        print(f"done repaired={repaired} skipped={skipped}", flush=True)


if __name__ == "__main__":
    main()
