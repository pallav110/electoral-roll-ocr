from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


API_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ocr_pdf_api.py"
SPEC = importlib.util.spec_from_file_location("ocr_pdf_api", API_PATH)
assert SPEC is not None and SPEC.loader is not None
ocr_pdf_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr_pdf_api)


def test_content_mask_keeps_only_serial_epic_and_hindi_regions() -> None:
    card = np.zeros((100, 200, 3), dtype=np.uint8)

    masked = ocr_pdf_api._mask_card_noise_for_tesseract(card)

    for x0_ratio, y0_ratio, x1_ratio, y1_ratio in ocr_pdf_api.TESSERACT_CARD_CONTENT_REGIONS:
        x0, x1 = int(card.shape[1] * x0_ratio), int(card.shape[1] * x1_ratio)
        y0, y1 = int(card.shape[0] * y0_ratio), int(card.shape[0] * y1_ratio)
        assert np.array_equal(masked[y0:y1, x0:x1], card[y0:y1, x0:x1])
    assert np.all(masked[95:, :] == 255)
    assert np.all(masked[:, :8] == 255)
    assert np.all(card == 0)


def test_serial_crop_can_opt_out_of_photo_mask() -> None:
    serial_crop = np.zeros((30, 60, 3), dtype=np.uint8)

    unchanged = ocr_pdf_api._preprocess_tesseract_card_image(
        serial_crop, mask_photo_box=False)

    assert np.array_equal(unchanged, serial_crop)
    assert unchanged is serial_crop
