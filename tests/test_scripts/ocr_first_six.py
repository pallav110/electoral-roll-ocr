#!/usr/bin/env python3
"""Save raw OCR for cards 1 through 6 using the measured page-3 boundaries."""

from pathlib import Path

import fitz

from pipeline.pdf_extract import _extract_text_with_tesseract
from OCR.tests.test_scripts.debug_first_three_ocr import PDF_PATH, PAGE_NUMBER, inner_rects, voter_card_rects

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "test_scripts" / "ocr_debug_output" / "first_six"
DPI = 200
CARD_COUNT = 6


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report: list[str] = []

    with fitz.open(PDF_PATH) as document:
        page = document[PAGE_NUMBER - 1]
        rectangles = voter_card_rects(page)[:CARD_COUNT]
        for card_number, (_, card_rect) in enumerate(rectangles, 1):
            card_pixmap = page.get_pixmap(dpi=DPI, clip=card_rect, alpha=False)
            card_bytes = card_pixmap.tobytes("png")
            card_path = OUTPUT_DIR / f"card_{card_number}.png"
            card_path.write_bytes(card_bytes)

            serial_rect = inner_rects(card_rect)
            serial_pixmap = page.get_pixmap(dpi=DPI * 2, clip=serial_rect, alpha=False)
            serial_bytes = serial_pixmap.tobytes("png")
            serial_path = OUTPUT_DIR / f"card_{card_number}_serial.png"
            serial_path.write_bytes(serial_bytes)

            card_lines = _extract_text_with_tesseract(card_bytes) or []
            serial_lines = _extract_text_with_tesseract(
                serial_bytes, mask_photo_box=False) or []
            report.append("\n" + "=" * 72)
            report.append(f"CARD {card_number}")
            report.append(f"CARD IMAGE: {card_path}")
            report.append(f"SERIAL IMAGE: {serial_path}")
            report.append("RAW FULL-CARD TESSERACT OCR:")
            report.extend(
                f"{line_number:02d}: {line}"
                for line_number, line in enumerate(card_lines, 1)
            )
            if not card_lines:
                report.append("<NO OCR TEXT>")
            report.append("RAW SERIAL-BOX TESSERACT OCR:")
            report.extend(
                f"{line_number:02d}: {line}"
                for line_number, line in enumerate(serial_lines, 1)
            )
            if not serial_lines:
                report.append("<NO OCR TEXT>")

    report_path = OUTPUT_DIR / "first_six_raw_ocr.txt"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Saved {CARD_COUNT} cards and raw OCR report: {report_path}")
    print("\n".join(report))


if __name__ == "__main__":
    main()
