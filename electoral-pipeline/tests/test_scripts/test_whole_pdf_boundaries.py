#!/usr/bin/env python3
"""Render and validate voter-card boundaries across the whole PDF."""

from pathlib import Path

import fitz

from pipeline.pdf_extract import _iter_voter_card_images, _pixmap_mostly_blank
from OCR.tests.test_scripts.debug_first_three_ocr import inner_rects, voter_card_rects

ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
OUTPUT_DIR = ROOT / "test_scripts" / "whole_pdf_boundary_output"
DPI = 90


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with fitz.open(PDF_PATH) as document:
        print(f"PDF pages: {len(document)}")
        for page_number, page in enumerate(document, 1):
            rects = voter_card_rects(page)
            nonblank = 0
            for _, clip in rects:
                pixmap = page.get_pixmap(dpi=140, clip=clip, alpha=False)
                if not _pixmap_mostly_blank(pixmap):
                    nonblank += 1

            for _, clip in rects:
                page.draw_rect(clip, color=(1, 0, 0), width=1)
                page.draw_rect(inner_rects(clip), color=(0, 0.6, 0), width=1)
            overlay = page.get_pixmap(dpi=DPI, alpha=False)
            overlay.save(OUTPUT_DIR / f"page_{page_number:02d}_boundaries.png")
            print(
                f"page={page_number:02d} cards={len(rects)} "
                f"nonblank={nonblank:02d} overlay=page_{page_number:02d}_boundaries.png"
            )


if __name__ == "__main__":
    main()
