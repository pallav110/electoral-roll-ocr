#!/usr/bin/env python3
"""Generate high-resolution contact sheets for manual house-number review."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import fitz
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PDF = ROOT / "OCR/input/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
AUDIT = ROOT / "OCR/results/house_number_engine_audit.json"
OUTPUT = ROOT / "OCR/results/house_manual_review_sheets"
CARDS_PER_SHEET = 10
DPI = 500


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    rows = audit["records"]
    document = fitz.open(PDF)
    font = ImageFont.load_default(size=22)
    rendered: list[tuple[dict, Image.Image]] = []

    for row in rows:
        page = document[int(row["page_number"]) - 1]
        card_rect = __import__("pipeline.ocr_pdf_api", fromlist=["_voter_card_rects"])._voter_card_rects(page)[
            int(row["card_number"]) - 1
        ]
        # Include the relation, house-number, and age lines. Context prevents a
        # neighbouring number from being mistaken for the house number.
        line_rect = fitz.Rect(
            card_rect.x0 + card_rect.width * 0.005,
            card_rect.y0 + card_rect.height * 0.27,
            card_rect.x0 + card_rect.width * 0.76,
            card_rect.y0 + card_rect.height * 0.79,
        )
        pixmap = page.get_pixmap(dpi=DPI, clip=line_rect, alpha=False)
        crop = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        rendered.append((row, crop))

    manifest: list[dict] = []
    for offset in range(0, len(rendered), CARDS_PER_SHEET):
        group = rendered[offset : offset + CARDS_PER_SHEET]
        width = max(image.width for _, image in group)
        label_height = 42
        row_height = max(image.height for _, image in group) + label_height
        sheet = Image.new("RGB", (width, row_height * len(group)), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (row, image) in enumerate(group):
            y = index * row_height
            label = (
                f"#{int(row['sno']):03d}  page {row['page_number']} card {row['card_number']}  "
                f"T={row['tesseract_house'] or '<blank>'}  "
                f"P={row['paddle_house'] or '<blank>'}  "
                f"selected={row['selected_house'] or '<blank>'}"
            )
            draw.text((8, y + 7), label, fill="black", font=font)
            sheet.paste(image, (0, y + label_height))
        first = int(group[0][0]["sno"])
        last = int(group[-1][0]["sno"])
        path = OUTPUT / f"house_review_{first:03d}_{last:03d}.png"
        sheet.save(path, optimize=True)
        manifest.append({"first_sno": first, "last_sno": last, "path": str(path)})

    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"sheets": len(manifest), "cards": len(rows), "output": str(OUTPUT)}))


if __name__ == "__main__":
    main()
