#!/usr/bin/env python3
"""Export page 3 using separate red-card and green-serial OCR passes."""

import json
import re
import time
from pathlib import Path

import fitz

from pipeline.pdf_extract import (
    _extract_paddle_card_metadata,
    _extract_relation_fallback_with_tesseract,
    _extract_tesseract_epic,
    _extract_text_with_tesseract,
    _voter_card_rects,
    parse_voter_box_from_ocr_lines,
)

ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
OUT_PATH = ROOT / "page3_ocr_results.json"
PAGE_NUMBER = 3

NAME_KEYS = ("voter_first_name", "voter_middle_name", "voter_sur_name")
RELATION_VALUE_KEYS = (
    "voter_husband_name", "voter_father_name",
    "voter_mother_name", "voter_other_name",
)


def _is_clean_hindi_value(value: str) -> bool:
    return bool(value) and not re.search(r"[A-Za-z0-9]", value)


def _merge_focused_name_and_relation(record: dict, focused: dict) -> None:
    """Merge only high-signal fields from the focused OCR pass."""
    focused_name = [focused.get(key, "") for key in NAME_KEYS]
    current_name = [record.get(key, "") for key in NAME_KEYS]
    if (
        focused_name[0]
        and focused_name[0] == current_name[0]
        and all(not value or _is_clean_hindi_value(value) for value in focused_name)
    ):
        for key, value in zip(NAME_KEYS, focused_name):
            record[key] = value

    relation = focused.get("relation_name", "")
    relation_values = [focused.get(key, "") for key in RELATION_VALUE_KEYS]
    populated = [value for value in relation_values if value]
    if relation and len(populated) == 1 and _is_clean_hindi_value(populated[0]):
        record["relation_name"] = relation
        for key, value in zip(RELATION_VALUE_KEYS, relation_values):
            record[key] = value


def main() -> None:
    started = time.perf_counter()
    records = []
    with fitz.open(PDF_PATH) as document:
        page = document[PAGE_NUMBER - 1]
        for card_index, card_rect in enumerate(_voter_card_rects(page), 1):
            # Keep the proven 200 DPI input for Hindi Tesseract OCR. Use the
            # higher-resolution render only for Paddle's small card metadata.
            hindi_bytes    = page.get_pixmap(dpi=200, clip=card_rect, alpha=False).tobytes("png")
            metadata_bytes = page.get_pixmap(dpi=300, clip=card_rect, alpha=False).tobytes("png")
            lines = _extract_text_with_tesseract(hindi_bytes)
            record = parse_voter_box_from_ocr_lines(lines or [])
            # Run a focused name/relation pass and merge only clean values.
            relation_lines = _extract_relation_fallback_with_tesseract(metadata_bytes)
            relation_lines = [line.replace("|", " ").strip() for line in relation_lines]
            relation_record = parse_voter_box_from_ocr_lines(relation_lines)
            _merge_focused_name_and_relation(record, relation_record)
            metadata = _extract_paddle_card_metadata(metadata_bytes)
            if record.get("empty"):
                record = {
                    "sno": "", "id_card_no": "", "gender": "", "age": "",
                    "house_no": "", "voter_first_name": "",
                    "voter_middle_name": "", "voter_sur_name": "",
                    "relation_name": "", "voter_husband_name": "",
                    "voter_father_name": "", "voter_mother_name": "",
                    "voter_other_name": "",
                }
            record["sno"] = metadata["sno"]
            # Dedicated grayscale Tesseract is primary; Paddle and the full-card
            # parse are fallbacks when the strict EPIC pattern is not recognised.
            tesseract_epic = _extract_tesseract_epic(hindi_bytes)
            record["id_card_no"] = (
                tesseract_epic
                or metadata["id_card_no"]
                or record.get("id_card_no", "")
            )
            paddle_house = metadata.get("house_no", "")
            tesseract_house = record.get("house_no", "")
            # House number arbitration:
            # - Tesseract slash already present → trust Tesseract by default.
            #   Paddle can hallucinate extra LEADING digits (e.g. "448/444",
            #   "48/829") which makes it appear longer but wrong.
            # - Exception: if Tesseract's value looks like a truncated prefix of
            #   Paddle's value (e.g. Tesseract "8/8" vs Paddle "8/81"), Paddle
            #   wins — Tesseract dropped trailing digits after the slash.
            # - If Tesseract has no slash but Paddle found a clean slash address,
            #   use Paddle (e.g. Tesseract truncated "1/1044" to "044").
            # - If Tesseract is empty, use Paddle.
            paddle_is_slash = re.fullmatch(r"\d{1,3}/\d+[A-Za-z\u0900-\u097F]*", paddle_house)
            tesseract_is_slash = "/" in tesseract_house
            if paddle_is_slash and not tesseract_is_slash:
                record["house_no"] = paddle_house
            elif paddle_is_slash and tesseract_is_slash:
                # Only prefer Paddle when Tesseract is a strict prefix of Paddle
                # (trailing truncation), not when Paddle has extra leading digits.
                if paddle_house.startswith(tesseract_house) and len(paddle_house) > len(tesseract_house):
                    record["house_no"] = paddle_house
                # else: keep Tesseract
            elif not tesseract_house:
                record["house_no"] = paddle_house
            record["is_deleted"] = bool(metadata["is_deleted"])
            if any(record.get(key) for key in ("sno", "id_card_no", "voter_first_name", "age", "is_deleted")):
                record["card_index"] = card_index
                records.append(record)

    OUT_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "records": len(records),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "nonempty_sno": sum(bool(record.get("sno")) for record in records),
        "sno_values": [record.get("sno", "") for record in records],
        "output": str(OUT_PATH),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
