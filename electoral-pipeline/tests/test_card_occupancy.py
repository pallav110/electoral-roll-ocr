"""Conservative, OCR-free card occupancy tests."""
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_pdf_api as A  # noqa: E402


def canvas():
    return np.full((300, 500, 3), 255, dtype=np.uint8)


def test_blank_card_is_blank():
    assert A._classify_card_occupancy(canvas()) == A.CARD_OCCUPANCY_BLANK


def _occupied_card():
    image = canvas()
    cv2.putText(image, "ABC123456", (300, 70), cv2.FONT_HERSHEY_SIMPLEX,
                1.2, (0, 0, 0), 2)
    cv2.putText(image, "NAME FATHER HOUSE", (40, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    cv2.rectangle(image, (30, 180), (300, 230), (0, 0, 0), 2)
    return image


def test_card_with_voter_ink_is_occupied():
    assert A._classify_card_occupancy(_occupied_card()) == A.CARD_OCCUPANCY_OCCUPIED


def test_sparse_card_is_occupied():
    image = canvas()
    cv2.putText(image, "ABC123456", (300, 70), cv2.FONT_HERSHEY_SIMPLEX,
                1.2, (0, 0, 0), 2)
    cv2.putText(image, "NAME FATHER HOUSE", (40, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    assert A._classify_card_occupancy(image) == A.CARD_OCCUPANCY_OCCUPIED


def test_border_only_card_is_uncertain_not_blank():
    image = canvas()
    cv2.rectangle(image, (8, 8), (492, 292), (0, 0, 0), 2)
    assert A._classify_card_occupancy(image) == A.CARD_OCCUPANCY_UNCERTAIN


def test_distributed_noise_is_uncertain_not_occupied():
    image = canvas()
    for x, y in ((40, 60), (120, 100), (200, 140), (280, 180)):
        cv2.rectangle(image, (x, y), (x + 4, y + 4), (0, 0, 0), -1)
    assert A._classify_card_occupancy(image) == A.CARD_OCCUPANCY_UNCERTAIN


def test_low_contrast_card_is_uncertain_not_blank():
    image = np.full((300, 500, 3), 210, dtype=np.uint8)
    cv2.putText(image, "ABC123456", (40, 90), cv2.FONT_HERSHEY_SIMPLEX,
                1.2, (180, 180, 180), 2)
    assert A._classify_card_occupancy(image) == A.CARD_OCCUPANCY_UNCERTAIN


def test_epic_region_is_the_signal_not_hindi_region():
    image = canvas()
    h, w = image.shape[:2]
    x0, y0, x1, y1 = A.TESSERACT_CARD_CONTENT_REGIONS[1]
    cv2.rectangle(image, (int(w*x0)+5, int(h*y0)+5),
                  (int(w*x0)+80, int(h*y0)+35), (0, 0, 0), -1)
    assert A._classify_card_occupancy(image) != A.CARD_OCCUPANCY_BLANK


def test_scan_streak_is_uncertain_not_occupied():
    image = canvas()
    cv2.line(image, (20, 120), (240, 120), (0, 0, 0), 1)
    assert A._classify_card_occupancy(image) == A.CARD_OCCUPANCY_UNCERTAIN


def test_photo_only_card_is_uncertain_or_occupied_not_blank():
    image = canvas()
    cv2.rectangle(image, (300, 50), (470, 250), (0, 0, 0), 3)
    cv2.putText(image, "PHOTO", (320, 160), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (0, 0, 0), 2)
    assert A._classify_card_occupancy(image) != A.CARD_OCCUPANCY_BLANK


def test_classifier_does_not_call_ocr(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("occupancy classifier called OCR")
    monkeypatch.setattr(A.pytesseract, "image_to_string", fail)
    monkeypatch.setattr(A.pytesseract, "image_to_data", fail)
    assert A._classify_card_occupancy(canvas()) == A.CARD_OCCUPANCY_BLANK


def test_scale_change_does_not_turn_ink_into_blank():
    image = _occupied_card()
    small = cv2.resize(image, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    assert A._classify_card_occupancy(image) == A.CARD_OCCUPANCY_OCCUPIED
    assert A._classify_card_occupancy(small) != A.CARD_OCCUPANCY_BLANK
