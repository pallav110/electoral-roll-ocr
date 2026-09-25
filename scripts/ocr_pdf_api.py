#!/usr/bin/env python3
"""Standalone FastAPI service for the validated electoral-roll OCR pipeline.

Run this file directly:

    python ocr_pdf_api.py

Or run it with Uvicorn:

    uvicorn ocr_pdf_api:app --host 0.0.0.0 --port 8082

Extract one inclusive page range:

    curl -X POST http://127.0.0.1:8082/ocr/extract \
      -F 'pdf_file=@roll.pdf' -F 'start_page=3' -F 'end_page=5'

Extract every voter-card page in the PDF:

    curl -X POST http://127.0.0.1:8082/ocr/extract \
      -F 'pdf_file=@roll.pdf' -F 'whole_pdf=true'

Start the sequential Celery worker (RabbitMQ broker):

    pip install 'celery==5.4.0'
    export OCR_CELERY_BROKER_URL='amqp://guest:guest@127.0.0.1:5672//'
    export OCR_BATCH_ALLOWED_ROOTS='/data/inbox:/data/more-rolls'
    python ocr_pdf_api.py worker

Run the API in a second terminal with the same environment variables:

    python ocr_pdf_api.py

The API and worker must see the same OCR_BATCH_WORK_DIR. On one machine the
default /tmp/ocr_pdf_jobs is sufficient; containers must mount a shared path.

Queue several uploaded PDFs:

    curl -X POST http://127.0.0.1:8082/ocr/batch/upload \
      -F 'pdf_files=@roll-1.pdf' -F 'pdf_files=@roll-2.pdf' \
      -F 'whole_pdf=true'

Queue every PDF in an allowed server-side directory:

    curl -X POST http://127.0.0.1:8082/ocr/batch/path \
      -F 'directory_path=/data/inbox' -F 'whole_pdf=true'

Read job progress:

    curl http://127.0.0.1:8082/ocr/jobs/JOB_ID

Run the built-in regression checks:

    python ocr_pdf_api.py self-test

Dataset-specific name corrections are disabled by default. If a verified roll
needs them, provide an explicit JSON mapping instead of editing the source:

    export OCR_NAME_TOKEN_CORRECTIONS_JSON='{"लाटयान":"लाट्यान","तुबार":"तुषार","नविश्ञ":"नविश"}'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

try:
    from celery import Celery
    from celery.result import AsyncResult
    CELERY_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    Celery = None
    AsyncResult = None
    CELERY_AVAILABLE = False

try:
    from paddleocr import PaddleOCR
    OCR_ENHANCEMENT_AVAILABLE = True
except ImportError:
    PaddleOCR = None
    OCR_ENHANCEMENT_AVAILABLE = False

log = logging.getLogger(__name__)


def _load_name_token_corrections() -> dict[str, str]:
    raw = os.getenv("OCR_NAME_TOKEN_CORRECTIONS_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "OCR_NAME_TOKEN_CORRECTIONS_JSON must be valid JSON") from exc
    if not isinstance(value, dict) or not all(
        isinstance(source, str) and isinstance(target, str)
        for source, target in value.items()
    ):
        raise RuntimeError(
            "OCR_NAME_TOKEN_CORRECTIONS_JSON must be a string-to-string object")
    return value


NAME_TOKEN_CORRECTIONS = _load_name_token_corrections()
_PADDLE_SERIAL_OCR = None

VOTER_CARD_ROW_BOUNDS = ((0.0325, 0.1209), (0.1266, 0.2155), (0.2213, 0.3099), (0.3155, 0.4042), (0.4094, 0.4982), (0.5035, 0.5921), (0.5975, 0.6863), (0.6917, 0.7806), (0.786, 0.8748), (0.8802, 0.969))
VOTER_CARD_LEFT_MARGIN = 0.011
VOTER_CARD_RIGHT_MARGIN = 0.019
VOTER_CARD_COLUMN_GAP = 0.0055



def _voter_card_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Return the measured voter-card rectangles for this page template."""
    rect = page.rect
    cols = 3
    x0 = rect.x0 + rect.width * VOTER_CARD_LEFT_MARGIN
    x1 = rect.x1 - rect.width * VOTER_CARD_RIGHT_MARGIN
    column_gap = rect.width * VOTER_CARD_COLUMN_GAP
    cell_w = (x1 - x0 - column_gap * (cols - 1)) / cols
    card_rects: list[fitz.Rect] = []
    for top_ratio, bottom_ratio in VOTER_CARD_ROW_BOUNDS:
        for col in range(cols):
            card_rects.append(fitz.Rect(
                x0 + col * (cell_w + column_gap),
                rect.y0 + rect.height * top_ratio,
                x0 + col * (cell_w + column_gap) + cell_w,
                rect.y0 + rect.height * bottom_ratio,
            ))
    return card_rects


def _extract_text_with_tesseract(img_bytes: bytes) -> Optional[list[str]]:
    """
    Extract text from image using Tesseract OCR with Hin+Eng language.
    Returns list of text lines or None if Tesseract is not available.
    """
    try:
        import cv2
        import pytesseract
        from PIL import Image
        import numpy as np

        # Convert bytes to numpy array
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            return None

        # Upscale and apply adaptive thresholding for clean OCR extraction
        img_resized = cv2.resize(img, (0, 0), fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)
        # Use adaptive threshold
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)

        candidates: list[list[str]] = []
        for variant in (gray, thresh):
            extracted_text = pytesseract.image_to_string(
                Image.fromarray(variant),
                lang='hin+eng',
                config=r'--oem 3 --psm 6',
            )
            lines = [line.strip() for line in extracted_text.split('\n') if line.strip()]
            if lines:
                candidates.append(lines)

        if not candidates:
            return None

        def candidate_score(lines: list[str]) -> int:
            text = " ".join(lines)
            score = len(re.findall(r"[\u0900-\u097F]", text))
            score += 20 * len(re.findall(r"(?:पिता|पति|माता|अन्य)\s*का?\s*नाम", text))
            score += 10 * len(re.findall(r"(?:नाम|मकान|आयु|लिंग)", text))
            score += 10 * len(re.findall(r"[A-Z]{2,4}\d{6,8}", text))
            return score

        return max(candidates, key=candidate_score)

    except Exception as e:
        log.debug(f"Tesseract OCR failed: {e}")
        return None


def _extract_relation_fallback_with_tesseract(img_bytes: bytes) -> list[str]:
    """Read name and relation lines with a focused 200-DPI-equivalent pass."""
    try:
        import cv2
        import pytesseract
        from PIL import Image
        import numpy as np

        raw = np.frombuffer(img_bytes, np.uint8)
        image = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
        if image is None:
            return []
        image = cv2.resize(
            image, None, fx=2 / 3, fy=2 / 3, interpolation=cv2.INTER_AREA)
        height, width = image.shape
        relation = image[
            int(height * 0.22):int(height * 0.52),
            int(width * 0.01):int(width * 0.78),
        ]
        relation = cv2.resize(
            relation, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(
            Image.fromarray(relation),
            lang="hin+eng",
            config="--oem 3 --psm 6",
        )
        return [line.strip() for line in text.splitlines() if line.strip()]
    except Exception as e:
        log.debug("Relation fallback OCR failed: %s", e)
        return []


def _extract_tesseract_epic(img_bytes: bytes) -> str:
    """Read a three-letter/seven-digit EPIC from its header region."""
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image

        image = cv2.imdecode(
            np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return ""
        height, width = image.shape
        epic = image[
            int(height * 0.04):int(height * 0.22),
            int(width * 0.58):int(width * 0.995),
        ]
        epic = cv2.resize(
            epic, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(
            Image.fromarray(epic),
            lang="eng",
            config="--oem 3 --psm 7",
        )
        compact = re.sub(r"[^A-Z0-9]", "", text.upper())
        match = re.search(r"[A-Z]{3}\d{7}", compact)
        return match.group(0) if match else ""
    except Exception as e:
        log.debug("Tesseract EPIC OCR failed: %s", e)
        return ""


def _extract_tesseract_house_candidates(img_bytes: bytes) -> list[str]:
    """Read slash-form house numbers from a focused numeric-only crop."""
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image

        image = cv2.imdecode(
            np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return []
        height, width = image.shape
        house = image[
            int(height * 0.34):int(height * 0.64),
            int(width * 0.01):int(width * 0.74),
        ]
        house = cv2.resize(
            house, None, fx=4 / 3, fy=4 / 3,
            interpolation=cv2.INTER_CUBIC,
        )
        variants = [
            house,
            cv2.threshold(
                house, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
        ]
        candidates: list[str] = []
        for variant in variants:
            text = pytesseract.image_to_string(
                Image.fromarray(variant),
                lang="eng",
                config=(
                    "--oem 3 --psm 6 "
                    "-c tessedit_char_whitelist=0123456789/"
                ),
            )
            candidates.extend(re.findall(r"\d{1,3}/\d{2,6}", text))
        return candidates
    except Exception as exc:
        log.debug("Focused Tesseract house OCR failed: %s", exc)
        return []


def _get_paddle_serial_ocr():
    global _PADDLE_SERIAL_OCR
    if _PADDLE_SERIAL_OCR is None:
        if not OCR_ENHANCEMENT_AVAILABLE or PaddleOCR is None:
            return None
        _PADDLE_SERIAL_OCR = PaddleOCR(lang="en", use_angle_cls=False, show_log=False)
    return _PADDLE_SERIAL_OCR


def _paddle_text(image: Any) -> list[tuple[str, float]]:
    ocr = _get_paddle_serial_ocr()
    if ocr is None:
        return []
    try:
        result = ocr.ocr(image, cls=False)
        return [
            (str(item[1][0]), float(item[1][1]))
            for item in (result[0] or [])
            if item and len(item) > 1 and len(item[1]) > 1
        ]
    except Exception as e:
        log.debug("PaddleOCR failed: %s", e)
        return []


def _detect_deleted_watermark(image: Any) -> bool:
    """Detect the diagonal DELETED stamp, including partial OCR readings.

    Paddle can read a stamp partially as ELETED, ETED, TED, or only ED. Short
    fragments are accepted only when their OCR box is strongly diagonal, which
    prevents ordinary horizontal text ending in ED from becoming a false hit.
    """
    try:
        import cv2

        ocr = _get_paddle_serial_ocr()
        if ocr is None:
            return False
        enlarged = cv2.resize(
            image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        result = ocr.ocr(enlarged, cls=False)
        for item in (result[0] or []):
            if not item or len(item) < 2 or len(item[1]) < 2:
                continue
            box = item[0]
            text = re.sub(r"[^A-Z]", "", str(item[1][0]).upper())
            confidence = float(item[1][1])
            watermark_text = (
                "DELETED" in text
                or "ELETED" in text
                or text in {"ETED", "TED", "ED"}
            )
            if not watermark_text or confidence < 0.85 or len(box) < 2:
                continue
            dx = float(box[1][0]) - float(box[0][0])
            dy = float(box[1][1]) - float(box[0][1])
            if abs(dy) / max(abs(dx), 1.0) >= 0.25:
                return True
        return False
    except Exception as exc:
        log.debug("Deleted-watermark OCR failed: %s", exc)
        return False


def _extract_paddle_card_metadata(img_bytes: bytes) -> dict[str, Any]:
    """Extract serial, EPIC, and deleted watermark from a high-resolution card."""
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {"sno": "", "id_card_no": "", "house_no": "", "is_deleted": False}
        height, width = image.shape[:2]
        serial_base = image[int(height * .04):int(height * .22), int(width * .03):int(width * .38)]
        epic_base = image[int(height * .06):int(height * .20), int(width * .72):int(width * .99)]
        house_base = image[int(height * .34):int(height * .64), int(width * .01):int(width * .74)]
        age_base = image[int(height * .62):int(height * .91), int(width * .01):int(width * .55)]

        serial_candidates: list[tuple[str, float]] = []
        # Right-aligned crop checked first — serials are right-aligned in their
        # box; the large blank left region causes Paddle to misread a single digit
        # (e.g. the two loops of 8 become "00"). A high-confidence hit here takes
        # priority over the multi-variant vote below.
        right_crop = image[int(height * .04):int(height * .22), int(width * .20):int(width * .50)]
        right_enlarged = cv2.resize(right_crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        right_hits = [
            ("".join(re.findall(r"\d", t)), s)
            for t, s in _paddle_text(right_enlarged)
            if re.fullmatch(r"\d{1,3}", "".join(re.findall(r"\d", t)))
        ]
        right_hits = [(d, s) for d, s in right_hits if d]
        high_conf_right = [(d, s) for d, s in right_hits if s >= 0.90]

        variants = [
            serial_base,
            cv2.threshold(cv2.cvtColor(serial_base, cv2.COLOR_BGR2GRAY), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
            cv2.adaptiveThreshold(cv2.cvtColor(serial_base, cv2.COLOR_BGR2GRAY), 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2),
            image[int(height * .06):int(height * .19), int(width * .10):int(width * .36)],
        ]
        for variant in variants:
            enlarged = cv2.resize(variant, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            for text, score in _paddle_text(enlarged):
                digits = "".join(re.findall(r"\d", text))
                if digits and len(digits) <= 3:
                    serial_candidates.append((digits, score))

        sno = ""
        if high_conf_right:
            # Single high-confidence hit from the right-aligned crop — trust it
            # directly rather than letting low-confidence duplicates from the wide
            # crop outvote it.
            sno = max(high_conf_right, key=lambda x: x[1])[0]
        elif serial_candidates:
            counts: dict[str, int] = {}
            for value, _ in serial_candidates:
                counts[value] = counts.get(value, 0) + 1
            repeated = [value for value, count in counts.items() if count >= 2]
            sno = max(repeated or [value for value, _ in serial_candidates], key=lambda value: max(score for candidate, score in serial_candidates if candidate == value))

        epic_candidates = []
        epic = cv2.resize(epic_base, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
        for text, score in _paddle_text(epic):
            match = re.search(r"[A-Z]{2,4}\d{6,8}", text.upper().replace(" ", ""))
            if match:
                epic_candidates.append((match.group(0), score))
        id_card_no = max(epic_candidates, key=lambda item: item[1])[0] if epic_candidates else ""

        house_candidates: list[tuple[str, float, bool]] = []
        for scale in (3, 5):
            house_image = cv2.resize(
                house_base, None, fx=scale, fy=scale,
                interpolation=cv2.INTER_CUBIC)
            for text, score in _paddle_text(house_image):
                slash_matches = re.findall(r"\d{1,3}/\d+", text)
                if slash_matches:
                    house_candidates.extend(
                        (value, score, False) for value in slash_matches)
                    continue
                digit_groups = [
                    value for value in re.findall(r"\d+", text)
                    if len(value) <= 6
                ]
                if digit_groups:
                    value = max(digit_groups, key=len)
                    # A malformed colon/dash is commonly how Paddle renders
                    # the printed "मकान संख्या:" label immediately before a
                    # real value (for example ".-1160"). Retain this only as
                    # supporting evidence for the later two-engine merge.
                    strong_format = bool(re.search(
                        rf"(?:^|[.:])\s*-\s*{re.escape(value)}(?!\d)", text,
                    ))
                    house_candidates.append((value, score, strong_format))
        if house_candidates:
            house_no, house_confidence, house_strong_format = max(
                house_candidates,
                key=lambda item: (
                    "/" in item[0],
                    len(re.sub(r"\D", "", item[0])),
                    item[1],
                ),
            )
            house_votes = sum(
                value == house_no for value, _, _ in house_candidates)
            house_strong_format = house_strong_format or any(
                value == house_no and strong
                for value, _, strong in house_candidates
            )
        else:
            house_no, house_confidence = "", 0.0
            house_votes, house_strong_format = 0, False

        age_candidates: list[tuple[str, float]] = []
        for scale in (3, 5):
            age_image = cv2.resize(
                age_base, None, fx=scale, fy=scale,
                interpolation=cv2.INTER_CUBIC)
            for text, score in _paddle_text(age_image):
                for digits in re.findall(r"\d{1,3}", text):
                    if 18 <= int(digits) <= 120:
                        age_candidates.append((digits, score))

        deleted = _detect_deleted_watermark(image)
        return {
            "sno": sno,
            "id_card_no": id_card_no,
            "house_no": house_no,
            "house_confidence": house_confidence,
            "house_votes": house_votes,
            "house_strong_format": house_strong_format,
            "age_candidates": age_candidates,
            "is_deleted": deleted,
        }
    except Exception as e:
        log.debug("Paddle card metadata failed: %s", e)
        return {"sno": "", "id_card_no": "", "house_no": "", "is_deleted": False}


_HOUSE_SUFFIX_CORRECTIONS = {'भी': 'बी', 'भ': 'ब', 'थी': 'बी', 'थ': 'ब', 'फी': 'फी'}
_HOUSE_FUSED_CORRECTIONS = {'डइ': 'इ'}
_HOUSE_SUFFIX_RE = re.compile('^(\\d+(?:/\\d+)?)\\s+(भी|भ|थी|थ|फी)$')
_HOUSE_FUSED_RE = re.compile('^(\\d+)([\\u0900-\\u097F]+)((?:-\\d+)?)$')


def _normalize_house_suffix(value: str) -> str:
    """Correct common Tesseract misreads of Devanagari letter suffixes in house numbers.

    Handles two patterns:
    - Space-separated: "7 भी"  →  "7 बी"
    - Fused directly:  "8डइ-526" → "8इ-526"
    """
    v = value.strip()

    # Space-separated suffix (e.g. "7 भी")
    m = _HOUSE_SUFFIX_RE.match(v)
    if m:
        digits, suffix = m.group(1), m.group(2)
        corrected = _HOUSE_SUFFIX_CORRECTIONS.get(suffix, suffix)
        return f"{digits} {corrected}"

    # Fused Devanagari suffix (e.g. "8डइ-526")
    mf = _HOUSE_FUSED_RE.match(v)
    if mf:
        digits, deva, rest = mf.group(1), mf.group(2), mf.group(3)
        corrected = _HOUSE_FUSED_CORRECTIONS.get(deva, deva)
        return f"{digits}{corrected}{rest}"
    return value


def _clean_house_no(value: str) -> str:
    """Strip trailing OCR noise from a house number value.

    Tesseract often appends lowercase letters, dashes, or punctuation that it
    reads from printed card borders or rules immediately after the house-number
    field.  Examples::

        "8/24 o—"    →  "8/24"
        "8/829 cael" →  "8/829"
        "2a"         →  "2"    (fused lowercase — DELETED watermark artifact)
        "7 बी"       →  "7 बी" (Devanagari suffix kept — valid address)
        "8/24A"      →  "8/24A" (uppercase suffix kept — valid address)

    Strategy:
    - A valid house suffix is an uppercase ASCII letter OR a Devanagari character.
    - Lowercase ASCII fused directly to digits (e.g. "2a") or space-separated
      after the address (e.g. "8/24 o") is OCR noise and is removed.
    """
    v = value.strip()
    # Strip space-separated lowercase ASCII noise at end (e.g. "8/24 o—", "8/829 cael")
    v = re.sub(r"\s+[a-z][a-z\s\-|.]*$", "", v)
    # Strip fused lowercase ASCII directly after digits (e.g. "2a" from DELETED watermark)
    v = re.sub(r"(\d+)[a-z]+$", r"\1", v)
    # Strip lone trailing punctuation/whitespace
    v = v.strip(" .:;|—-")
    return v


def _is_valid_age(value: Any) -> bool:
    """Accept a canonical electoral-roll age from 18 through 120."""
    text = str(value or "").strip()
    return bool(
        re.fullmatch(r"[1-9]\d{1,2}", text)
        and 18 <= int(text) <= 120
    )


def parse_voter_box_from_ocr_lines(lines: list[str]) -> dict[str, Any]:
    """Rule-based parsing of a single OCR'd voter card.

    Each field is extracted by label and truncated at the next known field marker so
    later labels such as "मकान संख्या" or "आयु" do not leak into the earlier name and
    father-name fields.
    """
    if not lines:
        return {"empty": True}

    cleaned: list[str] = []
    for raw in lines:
        if not isinstance(raw, str):
            continue
        text = " ".join(raw.strip().split())
        text = text.replace("—", " ").replace("_", " ")
        text = text.strip(" .:;|[](){}<>")
        if text:
            cleaned.append(text)

    if not cleaned:
        return {"empty": True}

    digits_map = {
        "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
        "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
    }

    def clean_value(value: Any) -> str:
        if value is None:
            return ""
        text = str(value)
        text = "".join(digits_map.get(ch, ch) for ch in text)
        # Strip zero-width characters (ZWJ U+200D, ZWNJ U+200C, ZWS U+200B etc.)
        # that Tesseract sometimes inserts inside Hindi conjunct consonants.
        text = re.sub(r"[\u200b-\u200f\u00ad]", "", text)
        # Removing a joiner can expose duplicate viramas (न्‍्टी -> न्टी).
        text = re.sub(r"\u094d{2,}", "\u094d", text)
        text = " ".join(text.split())
        # Hindi colons are often recognised as a visarga or punctuation token
        # ("ः", "!"). Remove those characters only at value boundaries.
        return re.sub(
            r"[\s.,;:'\"“”‘’!！?¿:：।ः|\[\](){}<>\-–—]+$", "",
            re.sub(
                r"^[\s.,;:'\"“”‘’!！?¿:：।ः|\[\](){}<>\-–—]+", "", text,
            ),
        )

    def cut_at_next_field(value: Any, markers: list[str]) -> str:
        text = clean_value(value)
        lowered = text.lower()
        for marker in markers:
            idx = lowered.find(marker.lower())
            if idx != -1:
                return clean_value(text[:idx])
        return text

    def fullname_parts(value: Any) -> list[str]:
        text = clean_value(value)
        if not text:
            return []
        return [
            NAME_TOKEN_CORRECTIONS.get(part, part)
            for part in re.split(r"\s+", text)
            if re.search(r"[A-Za-z0-9\u0904-\u0939]", part)
        ]

    def parse_relation(raw_line: str) -> tuple[str, str]:
        line = clean_value(raw_line)
        if not line:
            return "", ""
        label_map = [
            ("पति", "पति का नाम"),
            ("पति", "प्रति का नाम"),
            ("पति", "पत्ति का नाम"),
            ("पति", "प्रत्ति का नाम"),
            ("पिता", "पिता का नाम"),
            ("पिता", "प्रिता का नाम"),
            ("माता", "माता का नाम"),
            ("अन्य", "अन्य का नाम"),
            ("पति", "Husband Name"),
            ("पिता", "Father Name"),
            ("माता", "Mother Name"),
            ("अन्य", "Other Name"),
            ("पति", "husband name"),
            ("पिता", "father name"),
            ("माता", "mother name"),
            ("अन्य", "other name"),
        ]
        for rel, label in label_map:
            idx = line.lower().find(label.lower())
            if idx != -1:
                after = line[idx + len(label):]
                name_val = cut_at_next_field(after, [
                    "मकान संख्या",
                    "मकानसंख्या",
                    "आयु",
                    "लिंग",
                    "फोटो",
                    "फ़ोटो",
                    "उपलब्ध है",
                    "उपलब्ध",
                    "House No",
                    "Age",
                    "Gender",
                ])
                # Strip leading punctuation that Tesseract reads from the colon/
                # visarga printed on the card (e.g. "ः मगेन्द्र" → "मगेन्द्र",
                # ", लखपत" → "लखपत").
                name_val = re.sub(
                    r"^[^A-Za-z0-9\u0904-\u0939]+", "", name_val
                ).strip()
                return rel, " ".join(
                    NAME_TOKEN_CORRECTIONS.get(part, part)
                    for part in name_val.split()
                )
        return "", ""

    full_text = " ".join(cleaned)
    full_text_norm = "".join(digits_map.get(ch, ch) for ch in full_text)

    record: dict[str, Any] = {
        "sno": "",
        "id_card_no": "",
        "gender": "",
        "age": "",
        "house_no": "",
        "voter_first_name": "",
        "voter_middle_name": "",
        "voter_sur_name": "",
        "relation_name": "",
        "voter_husband_name": "",
        "voter_father_name": "",
        "voter_mother_name": "",
        "voter_other_name": "",
    }

    id_match = re.search(r"([A-Z]{2,4}\d{6,8}|[A-Z]{3}\d{5,8})", full_text_norm)
    if id_match:
        record["id_card_no"] = id_match.group(1)

    for line in cleaned:
        m = re.search(r"\|\s*(\d{1,3})\s*\|", clean_value(line))
        if m:
            record["sno"] = m.group(1)
            break

    for raw in cleaned:
        line = clean_value(raw)
        if not line:
            continue

        if re.search(r"(?i)(?:पति|प्रति|पत्ति|प्रत्ति)\s*का\s*नाम|(?:पिता|प्रिता|माता|अन्य)\s*का\s*नाम|Husband\s*Name|Father\s*Name|Mother\s*Name|Other\s*Name|husband\s*name|father\s*name|mother\s*name|other\s*name", line):
            rel, rel_name = parse_relation(line)
            if rel:
                record["relation_name"] = rel
                if rel == "पति":
                    record["voter_husband_name"] = rel_name
                elif rel == "पिता":
                    record["voter_father_name"] = rel_name
                elif rel == "माता":
                    record["voter_mother_name"] = rel_name
                else:
                    record["voter_other_name"] = rel_name
            continue

        if re.search(r"(?i)^(?:नाम|name)\s*[:：;；]", line):
            after = re.split(r"[:：;；]", line, maxsplit=1)[1]
            after = re.sub(r"(?i)^(?:नाम|name)\s*[:：;；]*\s*", "", after)
            after = cut_at_next_field(after, [
                "मकान संख्या",
                "मकानसंख्या",
                "आयु",
                "लिंग",
                "फोटो",
                "फ़ोटो",
                "उपलब्ध है",
                "उपलब्ध",
                "House No",
                "Age",
                "Gender",
            ])
            name_parts = fullname_parts(after)
            if name_parts:
                record["voter_first_name"] = name_parts[0]
            if len(name_parts) == 2:
                record["voter_sur_name"] = name_parts[1]
            elif len(name_parts) > 2:
                record["voter_middle_name"] = name_parts[1]
                record["voter_sur_name"] = " ".join(name_parts[2:])
            continue

        house_label = r"(?:मकान|मक्कान|भ्रकान)\s*संख्या"
        if re.search(rf"(?i)(?:{house_label}|House\s*No|house\s*no)", line):
            after = line.split(":", 1)[1] if ":" in line else line
            after = re.sub(
                rf"(?i)^(?:{house_label}|House\s*No|house\s*no)\s*[|:：]*\s*",
                "", after,
            )
            # A narrow printed digit 1 is occasionally recognised as a lone
            # card-rule glyph. Only accept it when it is the entire value
            # immediately before the next known field.
            lone_one = bool(re.match(
                r"^\s*[|\[\]]\s+(?:फोटो|फ़ोटो|उपलब्ध|Photo)(?=\s|$)",
                after, flags=re.IGNORECASE,
            ))
            after = cut_at_next_field(after, ["आयु", "Age", "लिंग", "Gender", "फोटो", "फ़ोटो", "उपलब्ध है", "उपलब्ध"])
            if after:
                house_match = re.search(
                    r"(?:[A-Za-z\u0900-\u097F]+-)?\d+(?:/\d+)?"
                    r"(?:\s*[A-Za-z\u0900-\u097F]+(?:-\d+)?)?",
                    after,
                )
                raw_house = house_match.group(0).strip() if house_match else after
                record["house_no"] = _clean_house_no(_normalize_house_suffix(raw_house))
            elif lone_one:
                record["house_no"] = "1"
            continue

        if re.search(r"(?i)(?:आयु|Age)", line):
            # Split on any separator Tesseract may produce: colon, semicolon, exclamation
            after = re.split(r"[：:；;！!]", line, maxsplit=1)[-1]
            after = re.sub(r"(?i)^(?:आयु|Age)\s*[；;：:！!]*\s*", "", after)
            num_match = re.search(r"\d{1,3}", after)
            if num_match and _is_valid_age(num_match.group(0)):
                record["age"] = num_match.group(0)
            continue

        if re.search(r"(?i)(?:लिंग|Gender)", line):
            gender_match = re.search(r"(?i)(पुरुष|महिला|तृतीय\s*लिंग|Male|Female|Other)", line)
            if gender_match:
                value = gender_match.group(1)
                value_map = {"Male": "पुरुष", "Female": "महिला", "Other": "तृतीय लिंग", "पुरुष": "पुरुष", "महिला": "महिला", "तृतीय लिंग": "तृतीय लिंग"}
                record["gender"] = value_map.get(value, value)
            continue

    if not record["age"]:
        age_match = re.search(r"(?i)(?:आयु|Age)\s*[；;：:！!]?\s*(\d{1,3})", full_text_norm)
        if age_match and _is_valid_age(age_match.group(1)):
            record["age"] = age_match.group(1)

    if not record["gender"]:
        gender_match = re.search(r"(?i)(पुरुष|महिला|तृतीय\s*लिंग|Male|Female|Other)", full_text_norm)
        if gender_match:
            value = gender_match.group(1)
            value_map = {"Male": "पुरुष", "Female": "महिला", "Other": "तृतीय लिंग", "पुरुष": "पुरुष", "महिला": "महिला", "तृतीय लिंग": "तृतीय लिंग"}
            record["gender"] = value_map.get(value, value)

    if not record["house_no"]:
        house_match = re.search(r"\d+(?:/\d+[A-Za-z\u0900-\u097F]*)?|\d+[A-Za-z\u0900-\u097F]?", full_text_norm)
        if house_match and not house_match.group(0).strip().isdigit():
            record["house_no"] = _clean_house_no(_normalize_house_suffix(house_match.group(0).strip()))

    # Name fallback: when the नाम label is garbled by Tesseract (e.g. "ary:" or
    # "Bcf] :"), the per-line parser misses it. Scan all lines for a pattern of
    # ASCII-noise + ":" followed by Devanagari text that is NOT a known field label.
    if not record["voter_first_name"]:
        _relation_labels = {"पिता", "पति", "माता", "अन्य", "प्रिता", "प्रति"}
        _field_labels = {"मकान", "आयु", "लिंग", "फोटो", "फ़ोटो", "उपलब्ध"}
        for raw in cleaned:
            # Line must contain a colon and have Devanagari after it
            if ":" not in raw:
                continue
            before, _, after_colon = raw.partition(":")
            after_colon = after_colon.strip()
            # Skip if it's a known relation or field line
            if any(lbl in after_colon for lbl in _field_labels):
                continue
            if any(lbl in before for lbl in _relation_labels | _field_labels):
                continue
            # Must have Devanagari content after the colon
            if not re.search(r"[\u0900-\u097F]", after_colon):
                continue
            # Skip relation lines (पिता/पति ka naam)
            if re.search(r"(?:पिता|पति|माता|अन्य)\s*का", raw):
                continue
            name_val = cut_at_next_field(after_colon, [
                "मकान संख्या", "मकानसंख्या", "आयु", "लिंग", "फोटो", "फ़ोटो",
                "उपलब्ध है", "उपलब्ध", "House No", "Age", "Gender",
            ])
            name_parts = fullname_parts(name_val)
            if name_parts:
                record["voter_first_name"] = name_parts[0]
                if len(name_parts) == 2:
                    record["voter_sur_name"] = name_parts[1]
                elif len(name_parts) > 2:
                    record["voter_middle_name"] = name_parts[1]
                    record["voter_sur_name"] = " ".join(name_parts[2:])
                break

    if any(record.get(key) for key in (
        "id_card_no", "sno", "voter_first_name", "voter_father_name",
        "age", "gender", "relation_name",
    )):
        return record

    return {"empty": True}

MAX_PDF_BYTES = int(os.getenv("MAX_OCR_PDF_BYTES", str(100 * 1024 * 1024)))
MAX_BATCH_FILES = int(os.getenv("MAX_OCR_BATCH_FILES", "100"))
MAX_BATCH_BYTES = int(
    os.getenv("MAX_OCR_BATCH_BYTES", str(1024 * 1024 * 1024)))
BATCH_WORK_DIR = Path(
    os.getenv("OCR_BATCH_WORK_DIR", "/tmp/ocr_pdf_jobs")
).expanduser().resolve()
OCR_OUTPUT_DIR = Path(
    os.getenv("OCR_OUTPUT_DIR", "results/new_runs/ocr_output")
).expanduser().resolve()
OCR_PROCESS_LOCK_FILE = Path(
    os.getenv("OCR_PROCESS_LOCK_FILE", "/tmp/ocr_pdf_api.lock")
).expanduser().resolve()


def _allowed_batch_roots() -> tuple[Path, ...]:
    configured = os.getenv("OCR_BATCH_ALLOWED_ROOTS", "").strip()
    raw_roots = configured.split(os.pathsep) if configured else [str(Path.cwd())]
    return tuple(Path(value).expanduser().resolve() for value in raw_roots if value)


@contextmanager
def _exclusive_ocr_process() -> Any:
    """Serialize OCR across API and Celery processes on the same host."""
    OCR_PROCESS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    with OCR_PROCESS_LOCK_FILE.open("a+b") as lock_handle:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)

            try:
                yield
            finally:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)

        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


CELERY_BROKER_URL = os.getenv(
    "OCR_CELERY_BROKER_URL",
    os.getenv("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672//"),
)
CELERY_RESULT_BACKEND = os.getenv("OCR_CELERY_RESULT_BACKEND", "rpc://")
if CELERY_AVAILABLE:
    celery_app = Celery(
        "electoral_roll_ocr",
        broker=CELERY_BROKER_URL,
        backend=CELERY_RESULT_BACKEND,
    )
    celery_app.conf.update(
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        worker_concurrency=1,
        task_reject_on_worker_lost=True,
        result_expires=int(os.getenv("OCR_CELERY_RESULT_EXPIRES", "86400")),
    )
else:
    celery_app = None

NAME_KEYS = ("voter_first_name", "voter_middle_name", "voter_sur_name")
RELATION_VALUE_KEYS = (
    "voter_husband_name", "voter_father_name",
    "voter_mother_name", "voter_other_name",
)

app = FastAPI(
    title="Electoral Roll OCR API",
    version="1.0.0",
    description="Local Tesseract + PaddleOCR voter-card extraction.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
_OCR_LOCK = asyncio.Lock()


def _is_clean_hindi_value(value: str) -> bool:
    """Return true for Hindi text containing at least one actual letter.

    Devanagari punctuation and combining marks occupy the same Unicode block,
    so a block-only check incorrectly accepted values such as a lone visarga.
    """
    stripped = str(value or "").strip()
    return bool(
        re.fullmatch(r"[\u0900-\u097F\s]+", stripped)
        and re.search(r"[\u0904-\u0939]", stripped)
    )


def _focused_relation_agrees_with_voter(
    record: dict[str, Any], current: str, focused: str,
) -> bool:
    """Use focused relation text when its surname independently agrees."""
    voter_surname = str(record.get("voter_sur_name", "")).strip()
    current_parts = current.split()
    focused_parts = focused.split()
    return bool(
        _is_clean_hindi_value(voter_surname)
        and focused_parts
        and focused_parts[-1] == voter_surname
        and (not current_parts or current_parts[-1] != voter_surname)
    )


def _merge_focused_name_and_relation(
    record: dict[str, Any],
    focused: dict[str, Any],
) -> dict[str, list[str]]:
    outcome: dict[str, list[str]] = {
        "changed_fields": [],
        "conflicts": [],
    }
    focused_name = [focused.get(key, "") for key in NAME_KEYS]
    current_name = [record.get(key, "") for key in NAME_KEYS]
    focused_relation_values = [
        focused.get(key, "") for key in RELATION_VALUE_KEYS]
    focused_surname = str(focused_name[-1] or "").strip()
    surname_has_relation_support = bool(
        focused_surname
        and any(
            str(value).split()[-1] == focused_surname
            for value in focused_relation_values
            if str(value).split()
        )
    )
    can_merge_name = (
        focused_name[0]
        and all(not value or _is_clean_hindi_value(value) for value in focused_name)
        and (
            not current_name[0]
            or focused_name[0] == current_name[0]
            or surname_has_relation_support
        )
    )
    if can_merge_name:
        if (
            current_name[0]
            and focused_name[0] != current_name[0]
        ):
            outcome["conflicts"].append(
                "voter_name_contextual_override")
        for key, value in zip(NAME_KEYS, focused_name):
            if value != record.get(key, ""):
                outcome["changed_fields"].append(key)
            record[key] = value
    elif (
        focused_name[0]
        and current_name[0]
        and focused_name != current_name
    ):
        outcome["conflicts"].append("voter_name_ocr_conflict")

    relation = focused.get("relation_name", "")
    relation_values = focused_relation_values
    populated_indexes = [
        index for index, value in enumerate(relation_values) if value]
    if relation and len(populated_indexes) == 1:
        index = populated_indexes[0]
        focused_value = relation_values[index]
        current_value = record.get(RELATION_VALUE_KEYS[index], "")
        # A focused fallback may fill a missing/noisy relation, but must not
        # overwrite valid primary Hindi with a conflicting OCR variant.
        relation_is_compatible = (
            not record.get("relation_name")
            or record.get("relation_name") == relation
        )
        if relation_is_compatible and _is_clean_hindi_value(focused_value) and (
            not current_value
            or not _is_clean_hindi_value(current_value)
            or _focused_relation_agrees_with_voter(
                record, current_value, focused_value)
        ):
            record["relation_name"] = relation
            for key, value in zip(RELATION_VALUE_KEYS, relation_values):
                if value != record.get(key, ""):
                    outcome["changed_fields"].append(key)
                record[key] = value
        elif (
            current_value
            and focused_value
            and current_value != focused_value
        ):
            outcome["conflicts"].append("relation_name_ocr_conflict")
    return outcome


def _empty_record() -> dict[str, Any]:
    return {
        "sno": "", "id_card_no": "", "gender": "", "age": "",
        "house_no": "", "voter_first_name": "",
        "voter_middle_name": "", "voter_sur_name": "",
        "relation_name": "", "voter_husband_name": "",
        "voter_father_name": "", "voter_mother_name": "",
        "voter_other_name": "",
    }


def _source_pdf_name(filename: str) -> str:
    """Return only the uploaded PDF basename, without client path data."""
    return os.path.basename(str(filename or "").replace("\\", "/")) or "document.pdf"


def _extract_state_code(page: fitz.Page) -> str:
    """Extract the two-digit state code from the page-1 S24 heading."""
    try:
        import cv2
        import numpy as np

        header_height = min(65.0, page.rect.height)
        header_clip = fitz.Rect(
            page.rect.x0, page.rect.y0,
            page.rect.x1, page.rect.y0 + header_height,
        )
        image_bytes = page.get_pixmap(
            dpi=300, clip=header_clip, alpha=False).tobytes("png")
        image = cv2.imdecode(
            np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return ""
        height, width = image.shape[:2]
        heading = image[
            int(height * 0.50):int(height * 0.92),
            int(width * 0.48):int(width * 0.60),
        ]
        heading = cv2.resize(
            heading, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        candidates: list[tuple[str, float]] = []
        for text, confidence in _paddle_text(heading):
            match = re.search(r"S\s*(\d{2})", text.upper())
            if match:
                candidates.append((match.group(1), confidence))
        if not candidates:
            return ""
        return max(candidates, key=lambda item: item[1])[0]
    except Exception as exc:
        log.warning("State-code extraction failed: %s", exc)
        return ""


def _extract_roll_header_metadata(page: fitz.Page) -> dict[str, str]:
    """Extract roll-wide codes and Hindi section name once from page 3."""
    empty = {
        "state_code": "",
        "ac_code": "",
        "anubhag_code": "",
        "anubhag_name": "",
        "booth_code": "",
    }
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image

        header_height = min(40.0, page.rect.height)
        header_clip = fitz.Rect(
            page.rect.x0, page.rect.y0,
            page.rect.x1, page.rect.y0 + header_height,
        )
        image_bytes = page.get_pixmap(
            dpi=300, clip=header_clip, alpha=False).tobytes("png")
        image = cv2.imdecode(
            np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return empty
        height, width = image.shape[:2]

        def crop(
            x0: float, y0: float, x1: float, y1: float, scale: float = 3,
        ) -> Any:
            region = image[
                int(height * y0):int(height * y1),
                int(width * x0):int(width * x1),
            ]
            return cv2.resize(
                region, None, fx=scale, fy=scale,
                interpolation=cv2.INTER_CUBIC)

        def paddle_number(region: Any) -> str:
            candidates: list[tuple[str, float]] = []
            for text, confidence in _paddle_text(region):
                for digits in re.findall(r"\d+", text):
                    candidates.append((digits, confidence))
            if not candidates:
                return ""
            return max(candidates, key=lambda item: (len(item[0]), item[1]))[0]

        # Ratios are measured against the 40-point page header at 300 DPI.
        ac_code = paddle_number(crop(0.282, 0.06, 0.363, 0.39))
        anubhag_code = paddle_number(
            crop(0.1573, 0.33, 0.1795, 0.671, scale=5))
        booth_code = paddle_number(crop(0.847, 0.06, 0.984, 0.39))

        anubhag_region = crop(0.016, 0.33, 0.363, 0.68)
        anubhag_text = pytesseract.image_to_string(
            Image.fromarray(cv2.cvtColor(anubhag_region, cv2.COLOR_BGR2GRAY)),
            lang="hin+eng",
            config="--oem 3 --psm 7",
        )
        anubhag_text = re.sub(r"[\u200b-\u200f\u00ad]", "", anubhag_text)
        anubhag_text = " ".join(anubhag_text.split())
        if ":" in anubhag_text:
            anubhag_text = anubhag_text.split(":", 1)[1]
        anubhag_name = re.sub(
            r"^[\s|:：;,.\-–—\d०-९]+", "", anubhag_text).strip()
        anubhag_name = anubhag_name.strip(" .:;|[](){}<>")

        return {
            "state_code": "",
            "ac_code": ac_code,
            "anubhag_code": anubhag_code,
            "anubhag_name": anubhag_name,
            "booth_code": booth_code,
        }
    except Exception as exc:
        log.warning("Roll header metadata extraction failed: %s", exc)
        return empty


def _split_person_name(full_name: str) -> tuple[str, str, str]:
    """Split a relation name while discarding punctuation-only OCR tokens."""
    parts = [
        part for part in str(full_name or "").split()
        if re.search(r"[A-Za-z0-9\u0900-\u097F]", part)
    ]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def _public_record(
    record: dict[str, Any], counter: int, pdf_name: str,
    roll_metadata: dict[str, str],
) -> dict[str, Any]:
    """Convert the internal OCR record to the public API schema."""
    relation_parts: dict[str, str] = {}
    for relation in ("husband", "father", "mother", "other"):
        first, middle, last = _split_person_name(
            record.get(f"voter_{relation}_name", ""))
        relation_parts[f"voter_{relation}_first_name"] = first
        relation_parts[f"voter_{relation}_middle_name"] = middle
        relation_parts[f"voter_{relation}_last_name"] = last

    return {
        "sno": counter,
        "voter_sr_no": str(record.get("sno", "")),
        "id_card_no": record.get("id_card_no", ""),
        "gender": record.get("gender", ""),
        "age": record.get("age", ""),
        "house_no": record.get("house_no", ""),
        "voter_first_name": record.get("voter_first_name", ""),
        "voter_middle_name": record.get("voter_middle_name", ""),
        "voter_sur_name": record.get("voter_sur_name", ""),
        "relation_name": record.get("relation_name", ""),
        **relation_parts,
        "is_deleted": bool(record.get("is_deleted", False)),
        "state_code": roll_metadata.get("state_code", ""),
        "ac_code": roll_metadata.get("ac_code", ""),
        "anubhag_code": roll_metadata.get("anubhag_code", ""),
        "anubhag_name": roll_metadata.get("anubhag_name", ""),
        "booth_code": roll_metadata.get("booth_code", ""),
        "pdf_name": pdf_name,
        "needs_review": bool(record.get("_needs_review", False)),
        "review_reasons": list(record.get("_review_reasons", [])),
        "field_sources": dict(record.get("_field_sources", {})),
    }


def _choose_house_number(
    tesseract_house: str,
    metadata: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """Reconcile house OCR engines and expose the decision provenance."""
    tesseract_house = str(tesseract_house or "")
    paddle_house = str(metadata.get("house_no", "") or "")
    confidence = float(metadata.get("house_confidence", 0.0))
    votes = int(metadata.get("house_votes", 0))
    strong_format = bool(metadata.get("house_strong_format", False))
    focused_counts = Counter(
        str(value)
        for value in metadata.get("focused_house_candidates", [])
    )
    reasons: list[str] = []

    if tesseract_house and paddle_house == tesseract_house:
        return tesseract_house, "tesseract+paddle", reasons

    paddle_is_slash = bool(re.fullmatch(
        r"\d{1,3}/\d+[A-Za-z\u0900-\u097F]*", paddle_house))
    tesseract_is_slash = "/" in tesseract_house
    if (
        paddle_is_slash
        and focused_counts[paddle_house] >= 2
        and confidence >= 0.70
    ):
        confirmed_house = paddle_house
        tesseract_match = re.fullmatch(
            r"([^\d]*)(\d+)/(\d+)", tesseract_house)
        paddle_parts = paddle_house.split("/", 1)
        if (
            tesseract_match
            and paddle_parts[1] == tesseract_match.group(3)
        ):
            confirmed_house = tesseract_match.group(1) + paddle_house
        return confirmed_house, "focused_tesseract+paddle", reasons
    if paddle_is_slash and not tesseract_house:
        if confidence >= 0.85 and votes >= 2:
            return paddle_house, "paddle_fallback", reasons
        reasons.append("unreliable_paddle_house")
        return "", "missing", reasons

    if paddle_is_slash and tesseract_is_slash:
        paddle_parts = paddle_house.split("/", 1)
        tesseract_match = re.fullmatch(
            r"([^\d]*)(\d+)/(\d+)", tesseract_house)
        if (
            tesseract_match
            and paddle_parts[1] == tesseract_match.group(3)
            and paddle_parts[0] == "1" + tesseract_match.group(2)
            and confidence >= 0.70
        ):
            return (
                tesseract_match.group(1) + paddle_house,
                "paddle_repair",
                reasons,
            )

    if (
        re.fullmatch(r"\d{1,4}", paddle_house)
        and re.fullmatch(r"\d{1,4}", tesseract_house)
        and confidence >= 0.75
    ):
        missing_leading_one = paddle_house == "1" + tesseract_house
        one_seven_confusion = (
            len(paddle_house) == len(tesseract_house)
            and sum(
                left != right
                for left, right in zip(paddle_house, tesseract_house)
            ) == 1
            and all(
                left == right or {left, right} == {"1", "7"}
                for left, right in zip(paddle_house, tesseract_house)
            )
        )
        independently_supported = strong_format and votes >= 2
        if (
            missing_leading_one
            or one_seven_confusion
            or independently_supported
        ):
            return paddle_house, "paddle_repair", reasons

    if (
        not tesseract_house
        and votes >= 2
        and (
            confidence >= 0.85
            or (strong_format and confidence >= 0.75)
        )
    ):
        return paddle_house, "paddle_fallback", reasons

    if tesseract_house:
        if (
            paddle_house
            and paddle_house != tesseract_house
            and confidence >= 0.70
        ):
            reasons.append("house_ocr_conflict")
        return tesseract_house, "tesseract", reasons
    reasons.append("missing_house_no")
    return "", "missing", reasons


def _choose_age(
    tesseract_age: str,
    raw_candidates: list[tuple[Any, Any]],
) -> tuple[str, str, list[str]]:
    """Reconcile plausible ages without broad look-alike substitution."""
    current = str(tesseract_age or "")
    if current and not _is_valid_age(current):
        current = ""
    candidates = [
        (str(value), float(confidence))
        for value, confidence in raw_candidates
        if _is_valid_age(value) and float(confidence) >= 0.55
    ]
    reasons: list[str] = []
    if current and any(value == current for value, _ in candidates):
        return current, "tesseract+paddle", reasons
    if current:
        corrections = [
            (value, confidence)
            for value, confidence in candidates
            if len(current) == 1 and value == "1" + current
        ]
        if corrections:
            return (
                max(corrections, key=lambda item: item[1])[0],
                "paddle_repair",
                reasons,
            )
        if candidates:
            reasons.append("age_ocr_conflict")
        return current, "tesseract", reasons
    unique_ages = {value for value, _ in candidates}
    if len(unique_ages) == 1:
        return next(iter(unique_ages)), "paddle_fallback", reasons
    if len(unique_ages) > 1:
        reasons.append("ambiguous_paddle_age")
    else:
        reasons.append("missing_age")
    return "", "missing", reasons


def _extract_card(
    page: fitz.Page,
    card_rect: fitz.Rect,
    page_number: Optional[int] = None,
    card_index: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    card_started = time.perf_counter()
    card_label = f"page={page_number or '?'} card={card_index or '?'}"
    stage_seconds: dict[str, float] = {}

    stage_started = time.perf_counter()
    hindi_bytes = page.get_pixmap(
        dpi=200, clip=card_rect, alpha=False).tobytes("png")
    metadata_bytes = page.get_pixmap(
        dpi=300, clip=card_rect, alpha=False).tobytes("png")
    stage_seconds["render"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    lines = _extract_text_with_tesseract(hindi_bytes)
    record = parse_voter_box_from_ocr_lines(lines or [])
    if record.get("empty"):
        record = _empty_record()
    stage_seconds["tesseract_primary"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    focused_lines = _extract_relation_fallback_with_tesseract(metadata_bytes)
    focused_lines = [
        re.sub(
            r"(?i)((?:पति|पिता|माता|अन्य)\s*का)\s*ATA\b",
            r"\1 नाम", line.replace("|", " ")
        ).strip()
        for line in focused_lines
    ]
    focused_record = parse_voter_box_from_ocr_lines(focused_lines)
    focused_outcome = _merge_focused_name_and_relation(
        record, focused_record)
    review_reasons = list(focused_outcome["conflicts"])
    stage_seconds["tesseract_focused"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    metadata = _extract_paddle_card_metadata(metadata_bytes)
    paddle_house_candidate = str(metadata.get("house_no", ""))
    primary_house_candidate = str(record.get("house_no", ""))
    if (
        "/" in paddle_house_candidate
        and paddle_house_candidate != primary_house_candidate
        and float(metadata.get("house_confidence", 0.0)) >= 0.70
        and int(metadata.get("house_votes", 0)) >= 2
    ):
        metadata["focused_house_candidates"] = (
            _extract_tesseract_house_candidates(metadata_bytes))
    stage_seconds["paddle_and_house_fallback"] = time.perf_counter() - stage_started

    record["sno"] = str(metadata.get("sno", ""))
    stage_started = time.perf_counter()
    strict_epic = _extract_tesseract_epic(hindi_bytes)
    stage_seconds["tesseract_epic"] = time.perf_counter() - stage_started
    paddle_epic = str(metadata.get("id_card_no", ""))
    parsed_epic = str(record.get("id_card_no", ""))
    record["id_card_no"] = strict_epic or paddle_epic or parsed_epic

    house_no, house_source, house_reasons = _choose_house_number(
        str(record.get("house_no", "")), metadata)
    record["house_no"] = house_no
    review_reasons.extend(house_reasons)

    age, age_source, age_reasons = _choose_age(
        str(record.get("age", "")),
        list(metadata.get("age_candidates", [])),
    )
    record["age"] = age
    review_reasons.extend(age_reasons)

    record["is_deleted"] = bool(metadata["is_deleted"])
    changed_fields = set(focused_outcome["changed_fields"])
    name_source = (
        "focused_tesseract"
        if changed_fields.intersection(NAME_KEYS)
        else ("tesseract" if record.get("voter_first_name") else "missing")
    )
    relation_source = (
        "focused_tesseract"
        if changed_fields.intersection(RELATION_VALUE_KEYS)
        else ("tesseract" if record.get("relation_name") else "missing")
    )
    field_sources = {
        "voter_sr_no": "paddle" if record.get("sno") else "missing",
        "id_card_no": (
            "focused_tesseract"
            if strict_epic else (
                "paddle" if paddle_epic else (
                    "tesseract" if parsed_epic else "missing"
                )
            )
        ),
        "voter_name": name_source,
        "relation_name": relation_source,
        "house_no": house_source,
        "age": age_source,
        "gender": "tesseract" if record.get("gender") else "missing",
        "is_deleted": "paddle",
    }
    required_fields = {
        "voter_sr_no": record.get("sno"),
        "id_card_no": record.get("id_card_no"),
        "voter_name": record.get("voter_first_name"),
        "relation_name": record.get("relation_name"),
        "house_no": record.get("house_no"),
        "age": record.get("age"),
        "gender": record.get("gender"),
    }
    review_reasons.extend(
        f"missing_{field}"
        for field, value in required_fields.items()
        if not value
    )
    record["_field_sources"] = field_sources
    record["_review_reasons"] = sorted(set(review_reasons))
    record["_needs_review"] = bool(record["_review_reasons"])
    has_record = any(record.get(key) for key in (
        "sno", "id_card_no", "voter_first_name", "age", "is_deleted",
    ))
    stage_report = " ".join(
        f"{name}={elapsed:.2f}s"
        for name, elapsed in stage_seconds.items()
    )
    print(
        f"[OCR] {card_label} {stage_report} "
        f"total={time.perf_counter() - card_started:.2f}s "
        f"result={'record' if has_record else 'empty'}",
        flush=True,
    )
    if not has_record:
        return None
    return record


def _page_looks_like_voter_grid(page: fitz.Page) -> bool:
    """Probe three expected card positions before running the expensive page OCR."""
    for card_rect in _voter_card_rects(page)[:3]:
        image = getattr(page, "get_pixmap")(
            dpi=200, clip=card_rect, alpha=False).tobytes("png")
        if _extract_tesseract_epic(image):
            return True
    return False


def _reconcile_page_serials(
    page_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Repair serial outliers using a majority-supported page sequence."""
    bases: list[int] = []
    for record in page_records:
        raw = str(record.get("sno", ""))
        card_index = record.get("_card_index")
        if raw.isdigit() and isinstance(card_index, int):
            base = int(raw) - card_index + 1
            if base > 0:
                bases.append(base)
    if not bases:
        return []

    base, support = Counter(bases).most_common(1)[0]
    if support * 2 <= len(page_records):
        return []

    corrections: list[dict[str, Any]] = []
    for record in page_records:
        expected = str(base + record["_card_index"] - 1)
        raw = str(record.get("sno", ""))
        if raw != expected:
            corrections.append({
                "_card_index": record["_card_index"],
                "ocr_value": raw,
                "corrected_value": expected,
            })
            record["sno"] = expected
            record.setdefault("_field_sources", {})[
                "voter_sr_no"] = "page_sequence_reconciliation"
            reasons = record.setdefault("_review_reasons", [])
            reasons.append("voter_serial_reconciled")
            record["_review_reasons"] = sorted(set(reasons))
            record["_needs_review"] = True
    return corrections


def _resolve_page_range(
    page_count: int,
    start_page: Optional[int],
    end_page: Optional[int],
    whole_pdf: bool,
) -> tuple[int, int, str]:
    if whole_pdf:
        if start_page is not None or end_page is not None:
            raise ValueError(
                "whole_pdf=true cannot be combined with start_page or end_page")
        return 1, page_count, "whole_pdf"
    if start_page is None:
        raise ValueError("start_page is required unless whole_pdf=true")
    if start_page < 1:
        raise ValueError("start_page must be >= 1")
    selected_end = start_page if end_page is None else end_page
    if selected_end < start_page:
        raise ValueError("end_page must be greater than or equal to start_page")
    if selected_end > page_count:
        raise ValueError(
            f"end_page {selected_end} exceeds PDF page count {page_count}")
    return start_page, selected_end, "page_range"


def _extract_pdf_ocr_unlocked(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    whole_pdf: bool = False,
    skip_non_voter_pages: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    source_pdf_name = _source_pdf_name(filename)
    print(
        f"[OCR] request_start file={source_pdf_name} bytes={len(pdf_bytes)}",
        flush=True,
    )
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Invalid or unreadable PDF: {exc}") from exc

    with document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        page_count = len(document)
        if page_count == 0:
            raise ValueError("PDF has no pages")
        first, last, mode = _resolve_page_range(
            page_count, start_page, end_page, whole_pdf)
        print(
            f"[OCR] selection mode={mode} pages={first}-{last} "
            f"skip_non_voter_pages={skip_non_voter_pages}",
            flush=True,
        )
        metadata_page_number = 3 if page_count >= 3 else 1
        metadata_started = time.perf_counter()
        roll_metadata = _extract_roll_header_metadata(
            document[metadata_page_number - 1])
        roll_metadata["state_code"] = _extract_state_code(document[0])
        print(
            f"[OCR] header_metadata page={metadata_page_number} "
            f"elapsed={time.perf_counter() - metadata_started:.2f}s",
            flush=True,
        )

        records: list[dict[str, Any]] = []
        processed_pages: list[dict[str, Any]] = []
        skipped_pages: list[dict[str, Any]] = []
        serial_corrections: list[dict[str, Any]] = []

        for page_number in range(first, last + 1):
            page = document[page_number - 1]
            probe_started = time.perf_counter()
            looks_like_voter_grid = (
                _page_looks_like_voter_grid(page)
                if skip_non_voter_pages else True
            )
            probe_elapsed = time.perf_counter() - probe_started
            print(
                f"[OCR] page={page_number} grid_probe="
                f"{probe_elapsed:.2f}s voter_grid={looks_like_voter_grid}",
                flush=True,
            )
            if not looks_like_voter_grid:
                skipped_pages.append({
                    "page_number": page_number,
                    "reason": "no EPIC found in the first three expected card positions",
                })
                continue

            page_started = time.perf_counter()
            page_records: list[dict[str, Any]] = []
            card_rects = _voter_card_rects(page)
            for card_index, card_rect in enumerate(card_rects, 1):
                record = _extract_card(
                    page, card_rect, page_number, card_index)
                if record is None:
                    continue
                record["_page_number"] = page_number
                record["_card_index"] = card_index
                page_records.append(record)

            page_serial_corrections = _reconcile_page_serials(page_records)
            serial_corrections.extend({
                "_page_number": page_number,
                **correction,
            } for correction in page_serial_corrections)
            records.extend(page_records)
            page_elapsed = time.perf_counter() - page_started
            processed_pages.append({
                "page_number": page_number,
                "records": len(page_records),
                "serial_corrections": len(page_serial_corrections),
                "elapsed_seconds": round(page_elapsed, 2),
            })
            print(
                f"[OCR] page={page_number} complete "
                f"cards={len(card_rects)} records={len(page_records)} "
                f"elapsed={page_elapsed:.2f}s",
                flush=True,
            )

    record_numbers = {
        (record["_page_number"], record["_card_index"]): counter
        for counter, record in enumerate(records, 1)
    }
    public_serial_corrections = [{
        "sno": record_numbers[(
            correction["_page_number"], correction["_card_index"]
        )],
        "ocr_value": correction["ocr_value"],
        "corrected_value": correction["corrected_value"],
    } for correction in serial_corrections]
    public_records = [
        _public_record(record, counter, source_pdf_name, roll_metadata)
        for counter, record in enumerate(records, 1)
    ]
    review_reason_counts = Counter(
        reason
        for record in public_records
        for reason in record["review_reasons"]
    )

    elapsed = time.perf_counter() - started
    print(
        f"[OCR] request_complete pages_processed={len(processed_pages)} "
        f"pages_skipped={len(skipped_pages)} records={len(public_records)} "
        f"elapsed={elapsed:.2f}s",
        flush=True,
    )
    return {
        "ok": True,
        "filename": source_pdf_name,
        "page_count": page_count,
        "roll_metadata": roll_metadata,
        "selection": {
            "mode": mode,
            "start_page": first,
            "end_page": last,
            "pages_requested": last - first + 1,
            "skip_non_voter_pages": skip_non_voter_pages,
        },
        "pages_processed": len(processed_pages),
        "pages_skipped": len(skipped_pages),
        "processed_page_details": processed_pages,
        "skipped_page_details": skipped_pages,
        "serial_corrections_count": len(public_serial_corrections),
        "serial_correction_details": public_serial_corrections,
        "records_count": len(public_records),
        "records_needing_review": sum(
            record["needs_review"] for record in public_records),
        "review_reason_counts": dict(sorted(review_reason_counts.items())),
        "elapsed_seconds": round(elapsed, 2),
        "records": public_records,
    }


def extract_pdf_ocr(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    whole_pdf: bool = False,
    skip_non_voter_pages: bool = True,
) -> dict[str, Any]:
    """Run one PDF while holding the cross-process Paddle/Tesseract lock."""
    with _exclusive_ocr_process():
        return _extract_pdf_ocr_unlocked(
            pdf_bytes,
            filename,
            start_page,
            end_page,
            whole_pdf,
            skip_non_voter_pages,
        )


def _validate_batch_selection(
    start_page: Optional[int],
    end_page: Optional[int],
    whole_pdf: bool,
    skip_non_voter_pages: bool,
) -> dict[str, Any]:
    if whole_pdf:
        if start_page is not None or end_page is not None:
            raise ValueError(
                "whole_pdf=true cannot be combined with start_page or end_page")
    else:
        if start_page is None:
            raise ValueError(
                "start_page is required unless whole_pdf=true")
        if start_page < 1:
            raise ValueError("start_page must be >= 1")
        if end_page is not None and end_page < start_page:
            raise ValueError(
                "end_page must be greater than or equal to start_page")
    return {
        "start_page": start_page,
        "end_page": end_page,
        "whole_pdf": whole_pdf,
        "skip_non_voter_pages": skip_non_voter_pages,
    }


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _run_pdf_batch(task: Any, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    items = list(payload["items"])
    selection = dict(payload["selection"])
    output_dir = Path(payload["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir.parent / "status.json"
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for index, item in enumerate(items, 1):
        filename = _source_pdf_name(item["filename"])
        progress = {
            "status": "processing",
            "current": index,
            "total": len(items),
            "filename": filename,
            "completed": len(completed),
            "failed": len(failed),
        }
        task.update_state(state="PROGRESS", meta=progress)
        _write_json_atomically(status_path, progress)
        try:
            source_path = Path(item["path"]).resolve(strict=True)
            if source_path.suffix.lower() != ".pdf":
                raise ValueError("source is not a PDF")
            size = source_path.stat().st_size
            if size == 0:
                raise ValueError("PDF file is empty")
            if size > MAX_PDF_BYTES:
                raise ValueError(
                    f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit")

            result = extract_pdf_ocr(
                source_path.read_bytes(),
                filename,
                selection["start_page"],
                selection["end_page"],
                selection["whole_pdf"],
                selection["skip_non_voter_pages"],
            )
            output_name = (
                f"{index:04d}_{Path(filename).stem}_ocr.json")
            output_path = output_dir / output_name
            _write_json_atomically(output_path, result)
            completed.append({
                "filename": filename,
                "output_file": str(output_path),
                "records_count": result["records_count"],
                "records_needing_review": result[
                    "records_needing_review"],
                "elapsed_seconds": result["elapsed_seconds"],
            })
        except Exception as exc:
            log.exception("Batch OCR failed for %s", filename)
            failed.append({
                "filename": filename,
                "error": f"{type(exc).__name__}: {exc}",
            })

    summary = {
        "ok": not failed,
        "status": "completed" if not failed else "completed_with_errors",
        "total": len(items),
        "completed": len(completed),
        "failed": len(failed),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "output_dir": str(output_dir),
        "results": completed,
        "errors": failed,
    }
    _write_json_atomically(status_path, summary)
    return summary


if celery_app is not None:
    @celery_app.task(
        bind=True,
        name="electoral_roll_ocr.process_pdf_batch",
    )
    def process_pdf_batch_task(
        task: Any, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Process all PDFs in one task, strictly one after another."""
        return _run_pdf_batch(task, payload)
else:
    process_pdf_batch_task = None


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": (
            "tesseract+paddleocr"
            if OCR_ENHANCEMENT_AVAILABLE else "tesseract"
        ),
        "busy": _OCR_LOCK.locked(),
        "max_pdf_mb": MAX_PDF_BYTES // (1024 * 1024),
        "paddleocr_available": OCR_ENHANCEMENT_AVAILABLE,
        "celery_available": CELERY_AVAILABLE,
        "batch_max_files": MAX_BATCH_FILES,
    }


@app.post("/ocr/extract")
async def ocr_extract_endpoint(
    pdf_file: UploadFile = File(..., description="Electoral-roll PDF"),
    start_page: Optional[int] = Form(
        default=None, description="First page, 1-based and inclusive"),
    end_page: Optional[int] = Form(
        default=None, description="Last page, 1-based and inclusive"),
    whole_pdf: bool = Form(
        default=False, description="Process the complete PDF; cannot be combined with a range"),
    skip_non_voter_pages: bool = Form(
        default=True, description="Skip pages without EPICs in the expected card grid"),
) -> dict[str, Any]:
    filename = pdf_file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported")

    raw = await pdf_file.read(MAX_PDF_BYTES + 1)
    if not raw:
        raise HTTPException(400, "PDF file is empty")
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(
            413, f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit")

    try:
        async with _OCR_LOCK:
            result = await asyncio.to_thread(
                extract_pdf_ocr,
                raw,
                filename,
                start_page,
                end_page,
                whole_pdf,
                skip_non_voter_pages,
            )
        safe_stem = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).stem,
        ).strip(" .") or "document"
        output_path = OCR_OUTPUT_DIR / (
            f"{uuid.uuid4().hex}_{safe_stem}_ocr.json")
        result["json_output_file"] = str(output_path)
        await asyncio.to_thread(_write_json_atomically, output_path, result)
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        log.exception("OCR PDF extraction failed")
        raise HTTPException(500, f"OCR extraction failed: {exc}") from exc


def _require_celery() -> None:
    if process_pdf_batch_task is None:
        raise HTTPException(
            503,
            "Celery is not installed. Run 'pip install celery' and start "
            "'python ocr_pdf_api.py worker'.",
        )


def _submit_batch(
    job_id: str,
    items: list[dict[str, str]],
    selection: dict[str, Any],
) -> dict[str, Any]:
    _require_celery()
    job_dir = BATCH_WORK_DIR / job_id
    output_dir = job_dir / "results"
    payload = {
        "items": items,
        "selection": selection,
        "output_dir": str(output_dir),
    }
    queued = {
        "ok": True,
        "status": "queued",
        "job_id": job_id,
        "total": len(items),
        "completed": 0,
        "failed": 0,
        "output_dir": str(output_dir),
    }
    _write_json_atomically(job_dir / "status.json", queued)
    try:
        process_pdf_batch_task.apply_async(args=[payload], task_id=job_id)
    except Exception as exc:
        failure = {
            **queued,
            "ok": False,
            "status": "queue_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_json_atomically(job_dir / "status.json", failure)
        raise HTTPException(
            503, f"Could not submit the batch to Celery: {exc}") from exc
    return {
        **queued,
        "status_url": f"/ocr/jobs/{job_id}",
    }


def _path_is_allowed(path: Path) -> bool:
    return any(
        path == root or path.is_relative_to(root)
        for root in _allowed_batch_roots()
    )


@app.post("/ocr/batch/upload", status_code=202)
async def ocr_batch_upload_endpoint(
    pdf_files: list[UploadFile] = File(
        ..., description="One or more electoral-roll PDFs"),
    start_page: Optional[int] = Form(default=None),
    end_page: Optional[int] = Form(default=None),
    whole_pdf: bool = Form(default=True),
    skip_non_voter_pages: bool = Form(default=True),
) -> dict[str, Any]:
    """Stage multiple uploads and process them sequentially in Celery."""
    _require_celery()
    try:
        selection = _validate_batch_selection(
            start_page, end_page, whole_pdf, skip_non_voter_pages)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not pdf_files:
        raise HTTPException(400, "At least one PDF is required")
    if len(pdf_files) > MAX_BATCH_FILES:
        raise HTTPException(
            413, f"Batch exceeds the {MAX_BATCH_FILES}-file limit")

    filenames = [
        _source_pdf_name(upload.filename or "document.pdf")
        for upload in pdf_files
    ]
    invalid = [name for name in filenames if not name.lower().endswith(".pdf")]
    if invalid:
        raise HTTPException(
            415, f"Only PDF files are supported: {', '.join(invalid)}")

    job_id = uuid.uuid4().hex
    input_dir = BATCH_WORK_DIR / job_id / "input"
    input_dir.mkdir(parents=True, exist_ok=False)
    items: list[dict[str, str]] = []
    total_bytes = 0
    try:
        for index, (upload, filename) in enumerate(
            zip(pdf_files, filenames), 1
        ):
            destination = input_dir / f"{index:04d}_{filename}"
            file_bytes = 0
            with destination.open("xb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > MAX_PDF_BYTES:
                        raise HTTPException(
                            413,
                            f"{filename} exceeds the "
                            f"{MAX_PDF_BYTES // (1024 * 1024)} MB file limit",
                        )
                    if total_bytes > MAX_BATCH_BYTES:
                        raise HTTPException(
                            413,
                            "Batch exceeds the configured total-byte limit",
                        )
                    handle.write(chunk)
            if file_bytes == 0:
                raise HTTPException(400, f"{filename} is empty")
            items.append({
                "filename": filename,
                "path": str(destination),
            })
    finally:
        for upload in pdf_files:
            await upload.close()

    return _submit_batch(job_id, items, selection)


@app.post("/ocr/batch/path", status_code=202)
async def ocr_batch_path_endpoint(
    directory_path: str = Form(
        ..., description="Server-side directory containing PDFs"),
    recursive: bool = Form(default=False),
    start_page: Optional[int] = Form(default=None),
    end_page: Optional[int] = Form(default=None),
    whole_pdf: bool = Form(default=True),
    skip_non_voter_pages: bool = Form(default=True),
) -> dict[str, Any]:
    """Queue PDFs from an allowed server-side directory."""
    _require_celery()
    try:
        selection = _validate_batch_selection(
            start_page, end_page, whole_pdf, skip_non_voter_pages)
        directory = Path(directory_path).expanduser().resolve(strict=True)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not directory.is_dir():
        raise HTTPException(422, "directory_path must point to a directory")
    if not _path_is_allowed(directory):
        allowed = ", ".join(str(root) for root in _allowed_batch_roots())
        raise HTTPException(
            403, f"Directory is outside OCR_BATCH_ALLOWED_ROOTS: {allowed}")

    candidates = directory.rglob("*") if recursive else directory.glob("*")
    pdf_paths = sorted(
        (
            path.resolve()
            for path in candidates
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: str(path).lower(),
    )
    pdf_paths = [path for path in pdf_paths if _path_is_allowed(path)]
    if not pdf_paths:
        raise HTTPException(404, "No PDF files were found")
    if len(pdf_paths) > MAX_BATCH_FILES:
        raise HTTPException(
            413,
            f"Found {len(pdf_paths)} PDFs; limit is {MAX_BATCH_FILES}",
        )
    total_bytes = sum(path.stat().st_size for path in pdf_paths)
    oversized = [
        path.name for path in pdf_paths if path.stat().st_size > MAX_PDF_BYTES]
    if oversized:
        raise HTTPException(
            413, f"PDFs exceed the per-file limit: {', '.join(oversized)}")
    if total_bytes > MAX_BATCH_BYTES:
        raise HTTPException(
            413, "Batch exceeds the configured total-byte limit")

    job_id = uuid.uuid4().hex
    items = [
        {"filename": path.name, "path": str(path)}
        for path in pdf_paths
    ]
    return _submit_batch(job_id, items, selection)


@app.get("/ocr/jobs/{job_id}")
async def ocr_job_status_endpoint(job_id: str) -> dict[str, Any]:
    """Return durable batch progress and the Celery terminal state."""
    _require_celery()
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "Invalid job ID")
    status_path = BATCH_WORK_DIR / job_id / "status.json"
    if status_path.is_file():
        try:
            with status_path.open(encoding="utf-8") as handle:
                status = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(500, f"Could not read job status: {exc}") from exc
    else:
        status = None

    try:
        celery_result = AsyncResult(job_id, app=celery_app)
        celery_state = celery_result.state
        celery_info = celery_result.info
    except Exception as exc:
        log.warning("Could not read Celery state for %s: %s", job_id, exc)
        celery_state = "UNAVAILABLE"
        celery_info = None
    if celery_state == "FAILURE":
        return {
            "ok": False,
            "status": "failed",
            "job_id": job_id,
            "error": str(celery_info),
        }
    if status is None and celery_state in {"PENDING", "UNAVAILABLE"}:
        raise HTTPException(404, "Job not found")
    return {
        **(status or {"status": celery_state.lower()}),
        "job_id": job_id,
        "celery_state": celery_state,
        "status_url": f"/ocr/jobs/{job_id}",
    }


def _run_self_tests() -> None:
    """Run deterministic regression fixtures without OCR models or a PDF."""
    checks = 0

    def check(condition: bool, label: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(label)

    for value in ("18", "19", "54", "99", "120"):
        check(_is_valid_age(value), f"valid age {value}")
    for value in ("", "0", "9", "005", "121", "999"):
        check(not _is_valid_age(value), f"invalid age {value}")

    parsed = parse_voter_box_from_ocr_lines([
        "नाम: आकाश लुहार",
        "पिता का नाम: गजेंद्र लुहार",
        "मकान संख्या: 1030",
        "आयु: 22 लिंग: पुरुष",
    ])
    check(parsed["voter_first_name"] == "आकाश", "primary name")
    check(parsed["voter_sur_name"] == "लुहार", "primary surname")
    check(parsed["voter_father_name"] == "गजेंद्र लुहार", "father name")
    check(parsed["house_no"] == "1030", "house parse")
    check(parsed["age"] == "22", "age parse")
    check(parsed["gender"] == "पुरुष", "gender parse")

    punctuation = parse_voter_box_from_ocr_lines([
        "नाम: शशी",
        "पति का नाम: ! रामनिवास",
        "मकान संख्या: 162",
        "आयु: 52 लिंग: महिला",
    ])
    check(
        punctuation["voter_husband_name"] == "रामनिवास",
        "leading punctuation",
    )
    visarga = parse_voter_box_from_ocr_lines([
        "नाम: उदाहरण",
        "पिता का नाम: ः मगेन्द्र",
        "आयु: 30 लिंग: पुरुष",
    ])
    check(visarga["voter_father_name"] == "मगेन्द्र", "leading visarga")
    lone_one = parse_voter_box_from_ocr_lines([
        "नाम: ममता देवी",
        "मकान संख्या : | फोटो उपलब्ध है",
        "आयु: 50 लिंग: महिला",
    ])
    check(lone_one["house_no"] == "1", "lone printed one")
    devanagari_age = parse_voter_box_from_ocr_lines([
        "नाम: सीमा", "आयु: २७ लिंग: महिला",
    ])
    check(devanagari_age["age"] == "27", "Devanagari age")
    for invalid_age in ("005", "121", "999"):
        invalid = parse_voter_box_from_ocr_lines([
            "नाम: सीमा", f"आयु: {invalid_age} लिंग: महिला",
        ])
        check(invalid["age"] == "", f"parser rejects age {invalid_age}")
    uncorrected = parse_voter_box_from_ocr_lines([
        "नाम: तुबार लाटयान", "आयु: 19 लिंग: पुरुष",
    ])
    check(
        uncorrected["voter_first_name"] == "तुबार"
        and uncorrected["voter_sur_name"] == "लाटयान",
        "no dataset-specific token replacement",
    )

    def house_meta(
        value: str, confidence: float, votes: int,
        strong: bool = False,
    ) -> dict[str, Any]:
        return {
            "house_no": value,
            "house_confidence": confidence,
            "house_votes": votes,
            "house_strong_format": strong,
        }

    check(
        _choose_house_number(
            "8/444", house_meta("448/444", 0.90, 2))[0] == "8/444",
        "reject prefixed slash noise",
    )
    check(
        _choose_house_number(
            "इ-0/602", house_meta("10/602", 0.90, 2))[0] == "इ-10/602",
        "repair slash leading one",
    )
    check(
        _choose_house_number(
            "030", house_meta("1030", 0.90, 2))[0] == "1030",
        "repair plain leading one",
    )
    check(
        _choose_house_number(
            "7053", house_meta("1053", 0.90, 2))[0] == "1053",
        "repair supported one-seven house",
    )
    check(
        _choose_house_number(
            "", house_meta("1160", 0.84, 2, True))[0] == "1160",
        "strong-format house fallback",
    )
    check(
        _choose_house_number(
            "", house_meta("419", 0.55, 1))[0] == "",
        "reject weak house fallback",
    )
    focused_house = house_meta("1/1044", 0.86, 2)
    focused_house["focused_house_candidates"] = ["1/1044", "1/1044"]
    check(
        _choose_house_number("7044", focused_house)
        == ("1/1044", "focused_tesseract+paddle", []),
        "focused house consensus overrides primary",
    )
    prefixed_house = house_meta("10/602", 0.90, 2)
    prefixed_house["focused_house_candidates"] = ["10/602", "10/602"]
    check(
        _choose_house_number("इ-0/602", prefixed_house)[0] == "इ-10/602",
        "focused house consensus preserves prefix",
    )

    check(_choose_age("9", [("19", 0.90)])[0] == "19", "age leading one")
    check(_choose_age("47", [("41", 0.90)])[0] == "47", "age conflict")
    check(_choose_age("", [("41", 0.90), ("41", 0.80)])[0] == "41",
          "unanimous age fallback")
    check(_choose_age("", [("41", 0.90), ("47", 0.90)])[0] == "",
          "ambiguous age fallback")

    primary = {
        **_empty_record(),
        "voter_first_name": "तुबार",
        "voter_sur_name": "लाटयान",
        "relation_name": "पिता",
        "voter_father_name": "नविश्ञ कुमार लाटयान",
    }
    focused = {
        **_empty_record(),
        "voter_first_name": "तुषार",
        "voter_sur_name": "लाट्यान",
        "relation_name": "पिता",
        "voter_father_name": "नविश कुमार लाट्यान",
    }
    outcome = _merge_focused_name_and_relation(primary, focused)
    check(primary["voter_first_name"] == "तुषार", "supported focused name")
    check(primary["voter_sur_name"] == "लाट्यान", "supported focused surname")
    check(
        primary["voter_father_name"] == "नविश कुमार लाट्यान",
        "supported focused relation",
    )
    check(bool(outcome["changed_fields"]), "focused provenance")

    audit_record = {
        **_empty_record(),
        "sno": "97",
        "id_card_no": "AWX6029706",
        "age": "19",
        "gender": "पुरुष",
        "house_no": "इ-10/602",
        "voter_first_name": "तुषार",
        "_needs_review": True,
        "_review_reasons": ["voter_serial_reconciled"],
        "_field_sources": {"voter_sr_no": "page_sequence_reconciliation"},
    }
    public = _public_record(audit_record, 1, "roll.pdf", {})
    check(public["needs_review"] is True, "public review flag")
    check(
        public["review_reasons"] == ["voter_serial_reconciled"],
        "public review reasons",
    )
    check(
        public["field_sources"]["voter_sr_no"]
        == "page_sequence_reconciliation",
        "public field provenance",
    )

    print(json.dumps({
        "ok": True,
        "checks": checks,
        "message": "Standalone OCR regression checks passed",
    }, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _run_self_tests()
    elif len(sys.argv) > 1 and sys.argv[1] == "worker":
        if celery_app is None:
            raise SystemExit(
                "Celery is required: pip install 'celery==5.4.0'")
        celery_app.worker_main([
            "worker",
            f"--loglevel={os.getenv('OCR_CELERY_LOGLEVEL', 'INFO')}",
            "--pool=solo",
            "--concurrency=1",
        ])
    else:
        import uvicorn

        uvicorn.run(
            app,
            host=os.getenv("OCR_API_HOST", "0.0.0.0"),
            port=int(os.getenv("OCR_API_PORT", "8082")),
        )