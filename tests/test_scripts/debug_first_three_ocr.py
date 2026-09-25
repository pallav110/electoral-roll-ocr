#!/usr/bin/env python3
"""Visualize the first three voter-card crops and print raw Tesseract OCR."""

from pathlib import Path

import fitz

from pipeline.pdf_extract import _extract_text_with_tesseract, _pixmap_mostly_blank

ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
OUTPUT_DIR = ROOT / "test_scripts" / "ocr_debug_output"
PAGE_NUMBER = 3
MAX_CARDS = 30
DPI = 200
SHOW_BOUNDARY_NUMBERS = True
GRID_LEFT_OFFSET = 0.004
ROW_BOUNDS = [
    (0.0325, 0.1209), (0.1266, 0.2155), (0.2213, 0.3099),
    (0.3155, 0.4042), (0.4094, 0.4982), (0.5035, 0.5921),
    (0.5975, 0.6863), (0.6917, 0.7806), (0.7860, 0.8748),
    (0.8802, 0.9690),
]
COLUMN_GAP = 0.0055


def voter_card_rects(page: fitz.Page) -> list[tuple[int, fitz.Rect]]:
    """Return the same 3-column by 10-row grid used by the OCR pipeline."""
    rect = page.rect
    cols = 3
    x0 = rect.x0 + rect.width * (0.015 - GRID_LEFT_OFFSET)
    x1 = rect.x1 - rect.width * (0.015 + GRID_LEFT_OFFSET)
    column_gap = rect.width * COLUMN_GAP
    cell_w = (x1 - x0 - column_gap * (cols - 1)) / cols

    result: list[tuple[int, fitz.Rect]] = []
    for row, (top_ratio, bottom_ratio) in enumerate(ROW_BOUNDS):
        for col in range(cols):
            index = row * cols + col + 1
            clip = fitz.Rect(
                x0 + col * cell_w,
                rect.y0 + rect.height * top_ratio,
                x0 + (col + 1) * cell_w,
                rect.y0 + rect.height * bottom_ratio,
            )
            clip.x0 += col * column_gap
            clip.x1 += col * column_gap
            result.append((index, clip))
    return result


def inner_rects(card: fitz.Rect) -> tuple[fitz.Rect, fitz.Rect]:
    """Return the serial-number and photo rectangles inside one voter card."""
    width = card.width
    height = card.height
    serial = fitz.Rect(
        card.x0 + width * 0.015,
        card.y0 + height * 0.04,
        card.x0 + width * 0.37,
        card.y0 + height * 0.21,
    )
    return serial


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selected: list[tuple[int, fitz.Rect, bytes]] = []
    report: list[str] = []

    with fitz.open(PDF_PATH) as document:
        page = document[PAGE_NUMBER - 1]
        for grid_index, clip in voter_card_rects(page):
            pixmap = page.get_pixmap(dpi=DPI, clip=clip, alpha=False)
            image_bytes = pixmap.tobytes("png")
            if _pixmap_mostly_blank(pixmap):
                continue
            selected.append((grid_index, clip, image_bytes))
            if len(selected) == MAX_CARDS:
                break

        if not selected:
            raise RuntimeError("No nonblank voter-card crops were found")

        # Draw on the in-memory page only; the source PDF is not modified.
        for crop_number, (grid_index, clip, _) in enumerate(selected, 1):
            serial_rect = inner_rects(clip)
            page.draw_rect(clip, color=(1, 0, 0), width=2)
            page.draw_rect(serial_rect, color=(0, 0.6, 0), width=2)
            if SHOW_BOUNDARY_NUMBERS:
                row = (grid_index - 1) // 3 + 1
                column = (grid_index - 1) % 3 + 1
                page.insert_text(
                    fitz.Point(clip.x0 + 4, clip.y1 - 4),
                    f"{grid_index} R{row}C{column}",
                    fontsize=8,
                    color=(0, 0, 1),
                    overlay=True,
                )

        marked_page = page.get_pixmap(dpi=120, alpha=False)
        marked_page.save(OUTPUT_DIR / "page3_first_three_marked.png")

    print(f"Saved marked page: {OUTPUT_DIR / 'page3_first_three_marked.png'}")

    with fitz.open(PDF_PATH) as document:
        page = document[PAGE_NUMBER - 1]
        for crop_number, (grid_index, card_rect, image_bytes) in enumerate(selected, 1):
            serial_rect = inner_rects(card_rect)
            card_path = OUTPUT_DIR / f"card_{crop_number}_grid_{grid_index}.png"
            card_path.write_bytes(image_bytes)
            lines = _extract_text_with_tesseract(image_bytes)
            serial_pixmap = page.get_pixmap(dpi=DPI * 2, clip=serial_rect, alpha=False)
            serial_bytes = serial_pixmap.tobytes("png")
            serial_path = OUTPUT_DIR / f"card_{crop_number}_grid_{grid_index}_serial.png"
            serial_path.write_bytes(serial_bytes)
            serial_lines = _extract_text_with_tesseract(
                serial_bytes, mask_photo_box=False)
            report.append("\n" + "=" * 72)
            report.append(f"CARD {crop_number} | GRID INDEX {grid_index}")
            report.append(f"Saved crop: {card_path}")
            report.append(f"Saved serial box: {serial_path}")
            report.append("RAW TESSERACT OCR:")
            if lines:
                for line_number, line in enumerate(lines, 1):
                    report.append(f"{line_number:02d}: {line}")
            else:
                report.append("<NO OCR TEXT>")
            report.append("SERIAL-BOX TESSERACT OCR:")
            if serial_lines:
                for line_number, line in enumerate(serial_lines, 1):
                    report.append(f"{line_number:02d}: {line}")
            else:
                report.append("<NO OCR TEXT>")

    report_path = OUTPUT_DIR / "raw_ocr.txt"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Saved raw OCR: {report_path}")
    print("\n".join(report))


if __name__ == "__main__":
    main()
