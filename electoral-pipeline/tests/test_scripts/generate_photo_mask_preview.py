#!/usr/bin/env python3
"""Render a before/after view of the Tesseract card-content mask."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "scripts" / "ocr_pdf_api.py"
DEFAULT_INPUT = ROOT / "results" / "ocr_debug_output" / "first_six" / "card_4.png"
DEFAULT_OUTPUT_DIR = ROOT / "results" / "ocr_debug_output" / "photo_mask"


def _load_ocr_api():
    spec = importlib.util.spec_from_file_location("ocr_pdf_api", API_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import OCR API from {API_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add_caption(image, text: str):
    return cv2.copyMakeBorder(
        image,
        38,
        8,
        0,
        0,
        cv2.BORDER_CONSTANT,
        value=(255, 255, 255),
    ), text


def _draw_caption(image, text: str) -> None:
    cv2.putText(
        image,
        text,
        (12, 27),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    original = cv2.imread(str(args.input), cv2.IMREAD_COLOR)
    if original is None:
        raise RuntimeError(f"Cannot read image: {args.input}")

    api = _load_ocr_api()
    masked = api._mask_card_noise_for_tesseract(original)
    if masked is None:
        raise RuntimeError("Could not apply card-content mask")

    height, width = original.shape[:2]
    annotated_original = original.copy()
    for x0_ratio, y0_ratio, x1_ratio, y1_ratio in api.TESSERACT_CARD_CONTENT_REGIONS:
        cv2.rectangle(
            annotated_original,
            (int(width * x0_ratio), int(height * y0_ratio)),
            (int(width * x1_ratio), int(height * y1_ratio)),
            (0, 0, 255),
            2,
        )
    annotated_original, original_caption = _add_caption(
        annotated_original, "Original card — kept OCR regions highlighted")
    masked_preview, masked_caption = _add_caption(
        masked, "Tesseract input — only kept OCR regions")
    _draw_caption(annotated_original, original_caption)
    _draw_caption(masked_preview, masked_caption)

    divider = 16
    preview = cv2.hconcat([
        annotated_original,
        255 * np.ones(
            (annotated_original.shape[0], divider, 3), dtype=annotated_original.dtype),
        masked_preview,
    ])

    args.output_dir.mkdir(parents=True, exist_ok=True)
    masked_path = args.output_dir / f"{args.input.stem}_tesseract_content_masked.png"
    preview_path = args.output_dir / f"{args.input.stem}_content_mask_before_after.png"
    if not cv2.imwrite(str(masked_path), masked):
        raise RuntimeError(f"Could not write {masked_path}")
    if not cv2.imwrite(str(preview_path), preview):
        raise RuntimeError(f"Could not write {preview_path}")

    print(f"Masked Tesseract input: {masked_path}")
    print(f"Before/after preview: {preview_path}")


if __name__ == "__main__":
    main()
