"""Pure shadow lattice inference tests."""
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_pdf_api as A  # noqa: E402


def _grid(height=1000, width=600, pitch=94, start=32):
    image = np.full((height, width), 255, dtype=np.uint8)
    for row in range(10):
        y0 = start + row * pitch
        cv2.rectangle(image, (10, y0), (width - 10, y0 + pitch - 12), 20, 2)
        cv2.putText(image, f"ROW{row}", (30, y0 + 35), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, 20, 2)
    return image


def test_lattice_shadow_infers_periodic_row_pitch():
    result = A._infer_lattice_shadow(_grid())
    assert result["status"] == "ok"
    assert abs(result["inferred_pitch_px"] - 94) <= 8
    assert result["harmonic"] in {"1x", "2x", "3x"}


def test_lattice_shadow_does_not_change_input():
    image = _grid()
    before = image.copy()
    A._infer_lattice_shadow(image)
    assert np.array_equal(image, before)


def test_lattice_shadow_is_uncertain_on_blank_page():
    result = A._infer_lattice_shadow(np.full((500, 300), 255, dtype=np.uint8))
    assert result["status"] == "uncertain"


def test_lattice_shadow_is_uncertain_on_tiny_page():
    result = A._infer_lattice_shadow(np.full((10, 10), 255, dtype=np.uint8))
    assert result["status"] == "uncertain"
