"""Targeted repair for blank ages in scanned rolls.

Renders only pages that contain a blank age and reads only the bounded
age/gender crop of the matching card.  Existing non-NULL values are never
modified.  The fix uses psm-7-only AGE_DIGIT_CONFIGS (psm 8 and 13 removed)
so there is no hallucination spike at 21.
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


def age_for_card(card_image: np.ndarray) -> str:
    clean = ocr._remove_outer_card_lines(card_image)
    return ocr._extract_age_from_clean_card(clean)


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT r.document_id, d.source_path, r.page_number, r.card_index
            FROM electoral_records r
            JOIN documents d ON d.id = r.document_id
            WHERE r.age IS NULL
              AND r.name_hi IS NOT NULL AND btrim(r.name_hi) <> ''
            ORDER BY d.source_path, r.page_number, r.card_index
        """)
        grouped: dict[tuple[object, str, int], list[int]] = defaultdict(list)
        for document_id, source_path, page_number, card_index in cur.fetchall():
            grouped[(document_id, source_path, page_number)].append(card_index)

        print(f"pages_to_scan={len(grouped)}", flush=True)
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
                    reads = list(pool.map(
                        lambda item: (item[0], age_for_card(item[1])), work
                    ))

                updates = [
                    (int(age_str), document_id, page_number, index)
                    for index, age_str in reads
                    if age_str and age_str.isdigit()
                    and ocr.AGE_MIN <= int(age_str) <= ocr.AGE_MAX
                ]
                if updates:
                    cur.executemany("""
                        UPDATE electoral_records
                        SET age = %s
                        WHERE document_id = %s AND page_number = %s AND card_index = %s
                          AND age IS NULL
                    """, updates)
                    repaired += cur.rowcount
                    conn.commit()
                pdf.close()
            except Exception as exc:
                print(f"page_failed source={source_path} page={page_number}: {exc}", flush=True)
            if repaired and repaired % 200 == 0:
                print(f"repaired={repaired}", flush=True)

        print(f"done repaired={repaired}", flush=True)


if __name__ == "__main__":
    main()
