import numpy as np

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ocr_pdf_api as A  # noqa: E402


def test_render_scale_diagnostic_is_uncertain_without_ink():
    result = A._card_render_scale_diagnostic(np.full((400, 600), 255, dtype=np.uint8))
    assert result["status"] == "uncertain"


def test_render_scale_diagnostic_reports_text_height():
    image = np.full((400, 600), 255, dtype=np.uint8)
    image[80:100, 60:540] = 20
    result = A._card_render_scale_diagnostic(image)
    assert result["status"] == "ok"
    assert result["text_height_px"] >= 15
    assert result["recommended_scale"] >= 1


def test_render_scale_never_downscales():
    image = np.full((400, 600), 255, dtype=np.uint8)
    image[80:160, 60:540] = 20
    result = A._card_render_scale_diagnostic(image, target_text_height=45)
    assert result["recommended_scale"] == 1
