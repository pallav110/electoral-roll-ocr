"""Targeted repair for blank genders in scanned rolls.

Renders only pages that contain a blank gender and reads only the bounded
age/gender crop of the matching card.  Existing nonblank values are never
modified.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import sys

import cv2
import fitz
import numpy as np
import psycopg

sys.path.insert(0, "/tmp")
import ocr_new as ocr

DATABASE_URL = "postgresql://electoral:electoral@db:5432/electoral"


def gender_for_card(card_image: np.ndarray) -> str:
    clean = ocr._remove_outer_card_lines(card_image)
    return ocr._extract_gender_from_clean_card(clean)


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT r.document_id, d.source_path, r.page_number, r.card_index
            FROM electoral_records r
            JOIN documents d ON d.id = r.document_id
            WHERE (r.gender_hi IS NULL OR btrim(r.gender_hi) = '')
              AND d.file_name <> 'JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf'
            ORDER BY d.source_path, r.page_number, r.card_index
        """)
        grouped: dict[tuple[object, str, int], list[int]] = defaultdict(list)
        for document_id, source_path, page_number, card_index in cur.fetchall():
            grouped[(document_id, source_path, page_number)].append(card_index)

        repaired = 0
        for (document_id, source_path, page_number), indexes in grouped.items():
            try:
                pdf = fitz.open(source_path)
                page = pdf[page_number - 1]
                pix = page.get_pixmap(dpi=200, alpha=False)
                image = np.frombuffer(pix.samples, np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                page_w, page_h = page.rect.width, page.rect.height
                cards = ocr._voter_card_rects(page)
                work = []
                for index in indexes:
                    # The scanned service numbers its cards from one; Python
                    # lists do not.  Keep the database identity unchanged.
                    slot = index - 1
                    if not 0 <= slot < len(cards):
                        continue
                    rect = cards[slot]
                    x0 = max(0, int(rect.x0 / page_w * pix.width))
                    y0 = max(0, int(rect.y0 / page_h * pix.height))
                    x1 = min(pix.width, int(rect.x1 / page_w * pix.width))
                    y1 = min(pix.height, int(rect.y1 / page_h * pix.height))
                    work.append((index, image[y0:y1, x0:x1].copy()))
                with ThreadPoolExecutor(max_workers=8) as pool:
                    reads = list(pool.map(lambda item: (item[0], gender_for_card(item[1])), work))
                # Explicit table, not a ternary: an `else "Female"` would file
                # a recovered तृतीय लिंग under Female.
                gender_en = {"पुरुष": "Male", "महिला": "Female",
                              "तृतीय लिंग": "Third Gender"}
                updates = [
                    (gender, gender_en[gender], document_id, page_number, index)
                    for index, gender in reads if gender in gender_en
                ]
                if updates:
                    cur.executemany("""
                        UPDATE electoral_records
                        SET gender_hi = %s, gender_en = %s
                        WHERE document_id = %s AND page_number = %s AND card_index = %s
                          AND (gender_hi IS NULL OR btrim(gender_hi) = '')
                    """, updates)
                    repaired += cur.rowcount
                    conn.commit()
                pdf.close()
            except Exception as exc:
                print(f"page_failed source={source_path} page={page_number}: {exc}", flush=True)
            if repaired and repaired % 250 == 0:
                print(f"repaired={repaired}", flush=True)
        print(f"done repaired={repaired}", flush=True)


if __name__ == "__main__":
    main()
