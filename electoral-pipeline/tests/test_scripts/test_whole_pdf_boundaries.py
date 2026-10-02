#!/usr/bin/env python3
"""Render and validate voter-card boundaries across the whole PDF.

    python tests/test_scripts/test_whole_pdf_boundaries.py [path/to.pdf]

Writes one overlay PNG per page to tests/test_scripts/whole_pdf_boundary_output/
and prints the card/nonblank counts, so a template change can be checked against
the drawn grid before anything is committed.

Three colours, all imported from the service rather than restated here:
    red    the card cell          _voter_card_rects
    green  the serial box         TESSERACT_CARD_CONTENT_REGIONS[SERIAL_REGION_INDEX]
    blue   the photo box          PHOTO_BOX_REGION

The imports below used to point at a `pipeline.pdf_extract` module and an
`OCR.tests...` path. Neither exists -- the layout was refactored into the
single-file ocr_pdf_api.py service, and the geometry helpers moved with it.
Every import here is now satisfied by a function that actually exists, and the
rects come from the service itself rather than a copy, so a calibration change
cannot leave this script drawing a grid the pipeline no longer uses.
"""

import argparse
import sys
from pathlib import Path

import fitz

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Every boundary this script draws is imported from the service, never restated
# here. _voter_card_rects is the function the pipeline slices cards with, and
# the two inner boxes are the crops it hands to OCR -- SERIAL_REGION_INDEX picks
# the serial box out of TESSERACT_CARD_CONTENT_REGIONS by name rather than by
# position, so the overlay cannot quietly disagree with the pipeline. This
# script previously carried its own copies of both boxes and they had already
# drifted from the service, which is precisely the failure an overlay exists
# to catch.
from ocr_pdf_api import (  # noqa: E402
    PHOTO_BOX_REGION,
    SERIAL_REGION_INDEX,
    TESSERACT_CARD_CONTENT_REGIONS,
    _voter_card_rects,
)

OUTPUT_DIR = REPO_ROOT / "tests" / "test_scripts" / "whole_pdf_boundary_output"
DEFAULT_PDF = REPO_ROOT / "sample-pdfs" / (
    "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
DPI = 90
PROBE_DPI = 140


def inner_rects(card: fitz.Rect) -> tuple[fitz.Rect, fitz.Rect]:
    """Return the serial-number and photo rectangles inside one voter card.

    Both are card-relative ratios, the same convention the service uses, so
    the overlay lines up with what OCR is actually shown.
    """
    def scaled(region: tuple[float, float, float, float]) -> fitz.Rect:
        return fitz.Rect(
            card.x0 + card.width * region[0], card.y0 + card.height * region[1],
            card.x0 + card.width * region[2], card.y0 + card.height * region[3],
        )

    return (scaled(TESSERACT_CARD_CONTENT_REGIONS[SERIAL_REGION_INDEX]),
            scaled(PHOTO_BOX_REGION))


INK_LEVEL = 200       # a sample darker than this counts as ink
INK_FRACTION = 0.01   # 1% of a cell's samples; blank paper is far below this


def looks_blank(page: fitz.Page, clip: fitz.Rect) -> bool:
    """True when a card cell holds essentially no ink.

    Counts dark samples rather than averaging. An average looks cheaper but is
    wrong twice over: pixmap.samples has one entry per *channel*, so dividing
    by width*height yields ~710 for white paper rather than 255, and even
    corrected, a mean is swamped by the paper. Counting ink is also what the
    question actually is -- "is anything drawn here" -- so it needs no tuning
    against a mean.
    """
    pixmap = page.get_pixmap(dpi=PROBE_DPI, clip=clip, alpha=False)
    total = pixmap.width * pixmap.height * pixmap.n
    if total == 0:
        return True
    ink = sum(1 for value in pixmap.samples if value < INK_LEVEL)
    return ink < total * INK_FRACTION


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", default=str(DEFAULT_PDF),
                        help="PDF to render (default: the sample roll)")
    parser.add_argument("--out", default=str(OUTPUT_DIR),
                        help=f"output directory (default: {OUTPUT_DIR})")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"no such PDF: {pdf_path}", file=sys.stderr)
        print("pass a path, e.g. "
              "python tests/test_scripts/test_whole_pdf_boundaries.py roll.pdf",
              file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with fitz.open(pdf_path) as document:
        page_count = document.page_count
        print(f"PDF: {pdf_path}")
        print(f"pages: {page_count}")
        for page_number, page in enumerate(document, 1):
            rects = _voter_card_rects(page)
            nonblank = sum(0 if looks_blank(page, clip) else 1 for clip in rects)

            for clip in rects:
                page.draw_rect(clip, color=(1, 0, 0), width=1)
                serial, photo = inner_rects(clip)
                page.draw_rect(serial, color=(0, 0.6, 0), width=1)
                page.draw_rect(photo, color=(0, 0, 1), width=1)

            overlay = page.get_pixmap(dpi=DPI, alpha=False)
            name = f"page_{page_number:02d}_boundaries.png"
            overlay.save(out_dir / name)
            print(f"page={page_number:02d} cards={len(rects)} "
                  f"nonblank={nonblank:02d} overlay={name}")

    print(f"\nwrote {page_count} overlays to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())