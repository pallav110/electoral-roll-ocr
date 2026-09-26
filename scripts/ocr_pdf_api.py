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
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, cast

import fitz
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
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
_PADDLE_INIT_LOCK = threading.Lock()
_PADDLE_INFERENCE_LOCK = threading.Lock()
try:
    _OCR_CARD_WORKERS = max(
        1,
        int(os.getenv("OCR_CARD_WORKERS", "1")),
    )
except ValueError:
    _OCR_CARD_WORKERS = 1
_OCR_FULL_PAGE_RENDER = os.getenv(
    "OCR_FULL_PAGE_RENDER", "0").strip().lower() in {"1", "true", "yes"}

VOTER_CARD_ROW_BOUNDS = ((0.0325, 0.1209), (0.1266, 0.2155), (0.2213, 0.3099), (0.3155, 0.4042), (0.4094, 0.4982), (0.5035, 0.5921), (0.5975, 0.6863), (0.6917, 0.7806), (0.786, 0.8748), (0.8802, 0.969))
VOTER_CARD_LEFT_MARGIN = 0.011
VOTER_CARD_RIGHT_MARGIN = 0.019
VOTER_CARD_COLUMN_GAP = 0.0055
TESSERACT_CARD_CONTENT_REGIONS = (
    (0.04, 0.02, 0.40, 0.23),
    (0.55, 0.02, 0.98, 0.23),
    (0.04, 0.22, 0.76, 0.94),
)
PHOTO_BOX_REGION = (0.75, 0.23, 0.99, 0.96)



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


def _render_page_card_images(
    page: fitz.Page,
    card_rects: list[fitz.Rect],
) -> Optional[list[tuple[bytes, bytes]]]:
    """Render a voter page twice, then slice all card images in memory.

    The two resolutions are intentional: Hindi Tesseract uses 200 DPI while
    numeric metadata and PaddleOCR use 300 DPI. Keeping the page render outside
    the card loop removes 60 individual PDF rasterizations per page without
    changing the card geometry or OCR inputs.
    """
    try:
        import cv2
        import numpy as np

        page_rect = page.rect
        rendered: list[list[bytes]] = []
        for dpi in (200, 300):
            pixmap = getattr(page, "get_pixmap")(
                dpi=dpi, alpha=False).tobytes("png")
            image = cv2.imdecode(
                np.frombuffer(pixmap, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.size == 0:
                raise ValueError(f"full-page {dpi} DPI render was empty")
            height, width = image.shape[:2]
            cards: list[bytes] = []
            for card_rect in card_rects:
                x0 = max(0, min(width - 1, round(
                    (card_rect.x0 - page_rect.x0) / page_rect.width * width)))
                y0 = max(0, min(height - 1, round(
                    (card_rect.y0 - page_rect.y0) / page_rect.height * height)))
                x1 = max(x0 + 1, min(width, round(
                    (card_rect.x1 - page_rect.x0) / page_rect.width * width)))
                y1 = max(y0 + 1, min(height, round(
                    (card_rect.y1 - page_rect.y0) / page_rect.height * height)))
                crop = image[y0:y1, x0:x1]
                ok, encoded = cv2.imencode(".png", crop)
                if not ok:
                    raise ValueError(f"could not encode {dpi} DPI card crop")
                cards.append(encoded.tobytes())
            rendered.append(cards)
        return list(zip(rendered[0], rendered[1]))
    except Exception as exc:
        log.warning("Full-page card rendering failed; using per-card fallback: %s", exc)
        return None


def _mask_card_noise_for_tesseract(image: Any) -> Any:
    """Keep the serial, EPIC, and Hindi field areas; whiten everything else."""
    import numpy as np

    if image is None or image.size == 0:
        return image
    height, width = image.shape[:2]
    masked = np.full_like(image, 255)
    for x0_ratio, y0_ratio, x1_ratio, y1_ratio in TESSERACT_CARD_CONTENT_REGIONS:
        x0, x1 = int(width * x0_ratio), int(width * x1_ratio)
        y0, y1 = int(height * y0_ratio), int(height * y1_ratio)
        masked[y0:y1, x0:x1] = image[y0:y1, x0:x1]
    return masked


def _preprocess_tesseract_card_image(
    image: Any,
    mask_photo_box: bool = True,
) -> Any:
    if not mask_photo_box:
        return image
    # Apply the standard card content masking first
    image = _mask_card_noise_for_tesseract(image)
    # Then mask the photo box area to prevent OCR of "उपलब्ध है" text
    # This ensures the photo box stays masked even if it overlaps with TESSERACT_CARD_CONTENT_REGIONS
    return _mask_photo_box(image)


def _mask_photo_box(image: Any) -> Any:
    """Remove the printed photo placeholder, including its label, from OCR input."""
    if image is None or image.size == 0:
        return image
    height, width = image.shape[:2]
    x0_ratio, y0_ratio, x1_ratio, y1_ratio = PHOTO_BOX_REGION
    x0, x1 = int(width * x0_ratio), int(width * x1_ratio)
    y0, y1 = int(height * y0_ratio), int(height * y1_ratio)
    masked = image.copy()
    masked[y0:y1, x0:x1] = 255
    return masked


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

        img = _preprocess_tesseract_card_image(img)

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
        image = _mask_photo_box(image)
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


def _extract_tesseract_age(img_bytes: bytes) -> str:
    """Read the small printed age digits from a focused numeric crop."""
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
        age = image[
            int(height * 0.55):int(height * 0.76),
            int(width * 0.04):int(width * 0.40),
        ]
        age = cv2.resize(
            age, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(
            Image.fromarray(age),
            lang="eng",
            config=(
                "--oem 3 --psm 8 "
                "-c tessedit_char_whitelist=0123456789"
            ),
        )
        match = re.search(r"\d{1,3}", text)
        return match.group(0) if match else ""
    except Exception as exc:
        log.debug("Focused Tesseract age OCR failed: %s", exc)
        return ""


def _extract_tesseract_house_prefix(img_bytes: bytes) -> str:
    """Read a short alphabetic prefix beside a focused slash-form house value."""
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
        house = image[
            int(height * 0.30):int(height * 0.62),
            int(width * 0.01):int(width * 0.76),
        ]
        house = cv2.resize(
            house, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(
            Image.fromarray(house),
            lang="hin+eng",
            config="--oem 3 --psm 6",
        )
        match = re.search(
            r"([$A-Za-z\u0904-\u0939]+)\s*-\s*(?=\d)", text)
        if not match:
            return ""
        prefix = match.group(1)
        # The printed prefix is short-i ``इ-``. Tesseract frequently
        # renders it as long-i ``ई`` or as an ASCII/symbol substitute.
        if (
            prefix in {"§", "8", "5", "इ", "ई", "$"}
            or prefix.lower() in {"e", "ee", "i", "ii"}
        ):
            return "इ-"
        return f"{prefix}-"
    except Exception as exc:
        log.debug("Focused Tesseract house-prefix OCR failed: %s", exc)
        return ""


def _choose_serial_value(tesseract_serial: Any, paddle_serial: Any) -> str:
    """Prefer a non-empty Paddle serial, otherwise preserve Tesseract's value."""
    paddle_value = str(paddle_serial or "").strip()
    if paddle_value:
        return paddle_value
    return str(tesseract_serial or "").strip()


def _extract_tesseract_house_address(img_bytes: bytes) -> str:
    """Read a complete multi-part plot/address value from a larger house crop."""
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
        house = image[
            int(height * 0.30):int(height * 0.62),
            int(width * 0.01):int(width * 0.76),
        ]
        house = cv2.resize(
            house, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(
            Image.fromarray(house),
            lang="hin+eng",
            config="--oem 3 --psm 11",
        )
        for line in text.splitlines():
            if not re.search(r"(?:पी\.?\s*नं|प्लॉट|प्लाट)", line):
                continue
            if ":" in line:
                line = line.split(":", 1)[1]
            parsed = parse_voter_box_from_ocr_lines([
                "नाम: परीक्षण",
                f"मकान संख्या: {line}",
            ])
            address = str(parsed.get("house_no", ""))
            if address.startswith(("पी", "प्लॉट", "प्लाट")):
                return address
        return ""
    except Exception as exc:
        log.debug("Focused Tesseract plot-address OCR failed: %s", exc)
        return ""


def _get_paddle_serial_ocr():
    global _PADDLE_SERIAL_OCR
    if _PADDLE_SERIAL_OCR is None:
        if not OCR_ENHANCEMENT_AVAILABLE or PaddleOCR is None:
            return None
        with _PADDLE_INIT_LOCK:
            if _PADDLE_SERIAL_OCR is None:
                _PADDLE_SERIAL_OCR = PaddleOCR(
                    lang="en", use_angle_cls=False, show_log=False)
    return _PADDLE_SERIAL_OCR


def _paddle_text(image: Any) -> list[tuple[str, float]]:
    ocr = _get_paddle_serial_ocr()
    if ocr is None:
        return []
    try:
        with _PADDLE_INFERENCE_LOCK:
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

        deleted = _detect_deleted_watermark(_mask_photo_box(image))
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
    # Normalize only the house-number prefix: the source glyph is short-i
    # ``इ``; OCR often emits ``ई`` or an ASCII/symbol look-alike. ``F`` is
    # another recurring OCR rendering when the glyph and its dash merge.
    v = re.sub(
        r"^(?:\$|§|=|8|5|ई|इ|F|(?:A\s+F)|[eEiI]{1,2})"
        r"\s*-\s*(?=\d|/)",
        "इ-", v, flags=re.IGNORECASE,
    )
    # OCR sometimes fuses the short-i prefix with the first digit, e.g.
    # ``F5/245`` for the printed ``इ-15/245``.
    v = re.sub(r"^(?:A\s+)?F\s*5/(\d+)$", r"इ-15/\1", v, flags=re.IGNORECASE)

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
    return v


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
        # The printed photo placeholder is not voter data.  It can survive
        # image masking in a fallback OCR pass. Drop photo-only lines here;
        # field parsers still truncate a trailing photo marker themselves so a
        # real value before it (including the printed one-glyph house value) is
        # preserved.
        if re.match(
            r"(?i)^(?:फोटो|फ़ोटो|Photo)\s+(?:उपलब्ध(?:\s+है)?|available)",
            text,
        ):
            continue
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
        parts = re.split(r"\s+", text)
        result: list[str] = []
        for part in parts:
            # Reject isolated single-character Devanagari fragments that are not
            # supported as legitimate standalone names (e.g. stray "\u0939" artifacts
            # from border/OCR noise), unless the whole name is a single character.
            if part == text and len(part) == 1 and re.fullmatch(r"[\u0904-\u0939]", part):
                # Single-character full name \u2014 keep only when it has actual
                # letter content (already ensured by regex below).
                pass
            # Only skip single-char fragments when the full text has multiple
            # parts (so a legitimate single-word name like "\u0938\u0941\u0928\u0940\u0924\u093e" isn't lost).
            if len(parts) > 1 and len(part) == 1 and part != text and re.fullmatch(r"[\u0904-\u0939]", part):
                # Skip isolated one-char trailing fragments that are likely OCR
                # artifacts, unless the full value is only that one character.
                continue
            if re.search(r"[A-Za-z0-9\u0904-\u0939]", part):
                result.append(NAME_TOKEN_CORRECTIONS.get(part, part))
        return result

    def parse_relation(raw_line: str) -> tuple[str, str]:
        line = clean_value(raw_line)
        if not line:
            return "", ""
        label_map = [
            ("पति", "पति का नाम"),
            ("पति", "पति कानाम"),
            ("पति", "पतिकानाम"),
            ("पति", "प्रति का नाम"),
            ("पति", "प्रति कानाम"),
            ("पति", "पत्ति का नाम"),
            ("पति", "पत्ति कानाम"),
            ("पति", "प्रत्ति का नाम"),
            ("पति", "प्रत्ति कानाम"),
            ("पिता", "पिता का नाम"),
            ("पिता", "पिता कानाम"),
            ("पिता", "पिताकानाम"),
            ("पिता", "प्रिता का नाम"),
            ("पिता", "प्रिता कानाम"),
            ("पिता", "पैता का नाम"),
            ("पिता", "पैता कानाम"),
            ("पिता", "fat का नाम"),
            ("माता", "माता का नाम"),
            ("माता", "माता कानाम"),
            ("अन्य", "अन्य का नाम"),
            ("अन्य", "अन्य कानाम"),
            ("पति", "Husband Name"),
            ("पिता", "Father Name"),
            ("माता", "Mother Name"),
            ("अन्य", "Other Name"),
            ("पति", "husband name"),
            ("पिता", "father name"),
            ("माता", "mother name"),
            ("अन्य", "other name"),
            ("पति", "पति"),
            ("पिता", "पिता"),
            ("माता", "माता"),
            ("अन्य", "अन्य"),
            ("पति", "प्रति"),
            ("पति", "पत्ति"),
            ("पिता", "प्रिता"),
            ("पति", "प्रत्ति"),
            ("पति", "Husband"),
            ("पिता", "Father"),
            ("माता", "Mother"),
            ("अन्य", "Other"),
            ("पति", "husband"),
            ("पिता", "father"),
            ("माता", "mother"),
            ("अन्य", "other"),
        ]
        for rel, label in label_map:
            idx = line.lower().find(label.lower())
            if idx != -1:
                if label in {"पति", "पिता", "माता", "अन्य", "प्रति", "पत्ति", "प्रita", "प्रत्ति",
                             "Husband", "Father", "Mother", "Other", "husband", "father", "mother",
                             "other"}:
                    after = line[idx + len(label):]
                    after = __import__("re").sub(r"^[：:।\s]*", "", after)
                else:
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

    def assign_relation(relation: str, name: str) -> None:
        """Store relation text in the aggregate key consumed by _public_record."""
        relation = clean_value(relation)
        name = clean_value(name)
        if relation not in {"पति", "पिता", "माता", "अन्य"}:
            return
        record["relation_name"] = relation
        record[f"voter_{({'पति': 'husband', 'पिता': 'father', 'माता': 'mother', 'अन्य': 'other'}[relation])}_name"] = name

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

        # Try raw (before clean_value transforms) for bare label split
        raw_text = raw.strip()
        raw_match = re.match(r"(?i)^\s*(अन्य|पिता|पति|माता)\s*[:：]\s*(.*)$", raw_text)
        if raw_match:
            raw_token = raw_match.group(1)
            after_raw = raw_match.group(2).strip()
            normalized_after = after_raw.lower()
            # Do not treat the normal '<relation> का नाम:' form as a bare
            # label; it must go through parse_relation below.
            if normalized_after.startswith(("का नाम", "का नाम:", "का", "name")):
                pass
            elif after_raw and normalized_after not in {"नाम", "name"}:
                assign_relation(raw_token, after_raw)
                continue
            elif not after_raw:
                assign_relation(raw_token, "")
                continue

        # Bare relation label with split name in same line (e.g. "अन्य: सुनीता")
        bare_rel_match = re.search(r"(?i)(?:अन्य|पिता|पति|माता)\s*[:：]\s*(.+)", line)
        if bare_rel_match:
            token = bare_rel_match.group(1)
            rel_candidate = "अन्य" if "अन्य" in line else ("पिता" if "पिता" in line else ("पति" if "पति" in line else ("माता" if "माता" in line else "")))
            if rel_candidate:
                name_after = bare_rel_match.group(1).strip()
                # Filter out label artifacts (e.g. "नाम") and keep real names
                if name_after and name_after.lower() not in {"नाम", "name", "का", "का नाम", "का नाम:", "का"} and len(name_after) > 1:
                    name_val = clean_value(name_after)
                    name_val = re.sub(r"^[^A-Za-z0-9ऄ-ह]+", "", name_val).strip()
                    assign_relation(rel_candidate, name_val)
            continue

        # Normal printed relation labels must be handled before the bounded
        # bare-label matcher below.  This dispatch used to sit after an
        # unconditional continue, making every standard relation unreachable.
        if re.search(
            r"(?i)(?:पति|प्रति|पत्ति|प्रत्ति)\s*(?:का\s*नाम|कानाम)|"
            r"(?:पिता|प्रिता|पैता|माता|अन्य)\s*(?:का\s*नाम|कानाम)|fat\s*का\s*नाम|"
            r"Husband\s*Name|Father\s*Name|Mother\s*Name|Other\s*Name|"
            r"husband\s*name|father\s*name|mother\s*name|other\s*name",
            line,
        ):
            rel, rel_name = parse_relation(line)
            if rel:
                assign_relation(rel, rel_name)
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

        # OCR commonly drops or substitutes characters in the printed
        # ``मकान संख्या`` label. Keep these variants bounded to the house
        # label so unrelated Hindi text cannot become a house candidate.
        house_label = (
            r"(?:मकान|मक्कान|भ्रकान|भकान|पकान|कान|Ta|ta|TH)\s*"
            r"(?:संख्या|संखा|सख्या|संख्य|deat)"
        )
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
                r"^\s*[|\[\]]\s*(?:(?:फोटो|फ़ोटो|Photo)\s+)?"
                r"(?:उपलब्ध(?:\s+है)?|available)?\s*$",
                after, flags=re.IGNORECASE,
            ))
            after = cut_at_next_field(after, ["आयु", "Age", "लिंग", "Gender", "फोटो", "फ़ोटो", "उपलब्ध है", "उपलब्ध"])
            # Normalize the value before extracting digits. OCR can render the
            # short-i prefix as ``§-0/602`` or fuse it with the leading digit
            # as ``हाऊस A F5/245``; extracting only the numeric suffix would
            # permanently lose the prefix and/or leading ``1``.
            after = re.sub(
                r"(?i)^(?:हाऊस|हाउस)\s*(?:नं\.?|A)?\s*",
                "",
                after,
            ).strip()
            after = _normalize_house_suffix(after)
            # ``§-0/602`` and ``$-70/602`` are common renderings of a
            # short-i-prefixed address whose leading 1 was lost. Keep the
            # conservative repair bounded to this OCR shape.
            after = re.sub(r"^इ-(?:0|70)/(?=\d)", "इ-10/", after)
            if after:
                # A bare border/photo token is the card's printed placeholder,
                # not a house value. Keep the narrow historical one-glyph repair.
                if re.fullmatch(
                    r"[|\[\]]?\s*(?:फोटो|फ़ोटो|उपलब्ध|Photo)\s*",
                    after,
                    flags=re.IGNORECASE,
                ):
                    if lone_one:
                        record["house_no"] = "1"
                    continue
                # Structured address preservation: multi-part plot/address
                # strings (e.g. plot numbers with prefixes, plot identifiers,
                # comma-separated sub-parts) must not be truncated to a single
                # numeric token.
                structured = after
                # Preserve full plot/plot-no patterns that contain prefixes,
                # commas, or multiple numeric sections.
                if re.search(r"(?:\s*[,;]\s*|\s+\u0916\s+|\s+\u0928\u0902\s+|\s+\u092A\u0940\.\s*\u0928\u0902|\s+\u092A\u094D\u0932(?:\u0949|\u094B)\u091F|\s+\u090F\u091A\u090F\u0928O|\s+\u0947\s*)", structured):
                    structured = structured
                # If after contains plot/plot identifier markers, keep the whole
                # string before falling back to numeric regex.
                has_plot_marker = bool(re.search(
                    r"(?:\u092A\u094D\u0932(?:\u0949|\u094B)\u091F|\u092A\u094D\u0932(?:\u0949|\u094B)\u091F\s*\u0928\u0902|\u092A\u0940\.\s*\u0928\u0902|\u090F\u091A\u090F\u0928O|\u0916\s+\d)", structured))
                has_comma_address = bool(
                    re.search(r"[,;]", structured)
                    and re.search(r"[A-Za-z\u0900-\u097F]", structured)
                )
                if has_plot_marker or has_comma_address:
                    # Preserve the complete structured address. Numeric-only
                    # Paddle candidates must not replace plot/address context.
                    record["house_no"] = _clean_house_no(_normalize_house_suffix(structured))
                else:
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
            elif num_match:
                record["_age_ocr_fragment"] = num_match.group(0)
            continue

        if re.search(r"(?i)(?:लिंग|Gender)", line):
            gender_match = re.search(
                r"(?i)(पुरुष|घुरुष|घुरूष|पुरुश|महिला|तृतीय\s*लिंग|Male|Female|Other)",
                line,
            )
            if gender_match:
                value = gender_match.group(1)
                value_map = {
                    "Male": "पुरुष", "Female": "महिला", "Other": "तृतीय लिंग",
                    "पुरुष": "पुरुष", "घुरुष": "पुरुष", "घुरूष": "पुरुष",
                    "पुरुश": "पुरुष", "महिला": "महिला", "तृतीय लिंग": "तृतीय लिंग",
                }
                record["gender"] = value_map.get(value, value)
            continue

    if not record["age"]:
        age_match = re.search(r"(?i)(?:आयु|Age)\s*[；;：:！!]?\s*(\d{1,3})", full_text_norm)
        if age_match and _is_valid_age(age_match.group(1)):
            record["age"] = age_match.group(1)

    if not record["gender"]:
        gender_match = re.search(
            r"(?i)(पुरुष|घुरुष|घुरूष|पुरुश|महिला|तृतीय\s*लिंग|Male|Female|Other)",
            full_text_norm,
        )
        if gender_match:
            value = gender_match.group(1)
            value_map = {
                "Male": "पुरुष", "Female": "महिला", "Other": "तृतीय लिंग",
                "पुरुष": "पुरुष", "घुरुष": "पुरुष", "घुरूष": "पुरुष",
                "पुरुश": "पुरुष", "महिला": "महिला", "तृतीय लिंग": "तृतीय लिंग",
            }
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

    # Multi-line bare-label repair is intentionally bounded to adjacent lines.
    # Do not scan the whole card: that can mistake the voter's own name or an
    # address fragment for the relation person.
    # Widened bare-label repair: even if relation_name is missing, scan for
    # bare relation tokens (other/father/husband/mother) followed by split
    # names, and fill all component fields. Also handle split names when
    # relation is present.
    candidates = []
    # 1. If relation_name missing, try to detect bare label + nearby name.
    rel_found = record.get("relation_name", "")
    # Look for bare relation tokens in full text if missing
    if not rel_found:
        for token in ["अन्य", "पिता", "पति", "माता"]:
            for raw in cleaned:
                cl = clean_value(raw)
                if cl == token or cl.startswith(token + ":") or (token in cl and ":" in cl and len(cl.split()) <= 3):
                    # Bare label with colon and possibly split name in same line
                    inferred_rel = token
                    # Try to extract split name from same line
                    after = cl[len(token):]
                    after = __import__("re").sub(r"^[：:。\s]*", "", after)
                    candidates.extend([after.strip()] if after.strip() else [])
    # Universal split-name recovery: for every canonical relation (detected or
    # inferred from nearby label lines), try to pick up split names.
    # Inference from label lines: if a line contains just the bare label or
    # label + colon, and nearby short Devanagari lines exist, infer rel.
    inferred_rel = rel_found or ""
    if not inferred_rel:
        for raw in cleaned:
            cl = clean_value(raw)
            if cl in {"अन्य", "पिता", "पति", "मMother"} or re.search(r"^(?:अन्य|पिता|पति|मMother)\s*[:：]\s*$", cl):
                # Prefer any that has a nearby short token line
                for raw2 in cleaned:
                    cl2 = clean_value(raw2)
                    if len(cl2.split()) <= 4 and cl2 != cl and re.search(r"[ऀ-ॿ]", cl2) and not re.search(r"(?:मकान|आयु|लिंग)", cl2):
                        if "अन्य" in cl: inferred_rel = "अन्य"
                        elif "पिता" in cl: inferred_rel = "पिता"
                        elif "पति" in cl: inferred_rel = "पति"
                        elif "मMother" in cl or "म" in cl and len(cl)<3: inferred_rel = "माता"
                        break
    # Apply repair with either detected or inferred relation
    target_rel = inferred_rel if inferred_rel else rel_found
    if target_rel:
        # Gather short token candidates from all lines (split names)
        candidates = []
        for index, raw in enumerate(cleaned):
            line = clean_value(raw)
            if index == 0 or not line:
                continue
            previous = clean_value(cleaned[index - 1])
            previous_compact = re.sub(r"\s+", "", previous)
            target_compact = re.sub(r"\s+", "", target_rel)
            valid_relation_line = previous in {
                target_rel, f"{target_rel}:", f"{target_rel}：",
            } or previous_compact in {
                target_compact,
                f"{target_compact}कानाम",
                f"{target_compact}कानाम:",
                f"{target_compact}कानाम：",
                f"{target_compact}कानाम",
            }
            if not valid_relation_line:
                continue
            if line == target_rel or re.search(
                r"(?i)(?:मकान|आयु|लिंग|फोटो|फ़ोटो|उपलब्ध|House|Age|Gender|नाम|Name)",
                line,
            ):
                continue
            if re.search(r"(?:पिता|पति|माता|अन्य)\s*[:：]", line):
                continue
            # Accept only a short, adjacent Devanagari name line.
            if re.fullmatch(
                r"[ऀ-ॿ]+(?:\s+[ऀ-ॿ]+){0,3}",
                line,
            ):
                candidates.append(line)
        # Also include lines that look like names but have extra ZWJ spaces to join
        # (fix split Devanagari fragments like ज़ुलफ़िक़् + आर -> joined)
        if candidates:
            # Deduplicate nearby candidates by joining fragments with ZWJ removed
            joined_candidates = []
            used = set()
            for c in candidates:
                key = c.replace("‍", "").replace("‌", "")
                if key not in used:
                    used.add(key)
                    joined_candidates.append(c)
            # Pick best: prefer ones with more Devanagari chars, exclude pure punctuation
            best = None
            for c in joined_candidates:
                if best is None or len(re.sub(r"[^ऀ-ॿ]", "", c)) > len(re.sub(r"[^ऀ-ॿ]", "", best or "")):
                    best = c
            if best:
                # Clean ZWJ / stray spaces inside words for Hindi join
                best_clean = best.replace(" ", "")
                # Actually keep spaces between words but remove stray ZWJ inside words
                best_clean = re.sub(r"([ऀ-ॿ])‍+([ऀ-ॿ])", r"\1\2", best)
                best_clean = re.sub(r"‍+", "", best_clean)
                best_clean = " ".join(best_clean.split())
                name_parts = fullname_parts(best_clean)
                if name_parts:
                    aggregate_key = {
                        "अन्य": "voter_other_name",
                        "पिता": "voter_father_name",
                        "पति": "voter_husband_name",
                        "माता": "voter_mother_name",
                    }.get(target_rel, "")
                    if aggregate_key and not record.get(aggregate_key):
                        record["relation_name"] = target_rel
                        record[aggregate_key] = " ".join(name_parts)
        # Relation names are recovered only from the immediately adjacent line
        # above. Never scan the rest of the card, because it may contain the
        # voter's own name or unrelated address text.

    # Do not infer a house number or relation from an unlabeled colon/name line.
    # House values must come from the explicit house label above or from the
    # multi-engine arbitration in _extract_card().

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
    celery_app = cast(Any, Celery)(
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
_cors_origins = tuple(
    origin.strip()
    for origin in os.getenv(
        "OCR_API_CORS_ORIGINS", "http://127.0.0.1:8082,http://localhost:8082"
    ).split(",")
    if origin.strip()
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"] if _cors_origins else [],
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
    current_name_tokens = [
        token for value in current_name for token in str(value or "").split()
    ]
    focused_name_tokens = [
        token for value in focused_name for token in str(value or "").split()
    ]
    # A focused crop can include a stray tail from the next/photo region
    # (e.g. ``शिवानी देवी हु`` while the primary pass reads ``शिवानी देवी``).
    # Do not replace a complete primary name with a longer focused token list
    # unless the relation surname independently supports that extra token.
    same_first_name_shape_is_safe = (
        focused_name[0] == current_name[0]
        and len(focused_name_tokens) <= len(current_name_tokens)
    )
    can_merge_name = (
        focused_name[0]
        and all(not value or _is_clean_hindi_value(value) for value in focused_name)
        and (
            not current_name[0]
            or same_first_name_shape_is_safe
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
        and all(_is_clean_hindi_value(v) for v in focused_name)
        and not all(_is_clean_hindi_value(v) for v in current_name)
    ):
        # Only flag a conflict when the focused fallback is cleaner than the
        # primary. When the focused name contains a digit/garbage (e.g. the
        # "मेता 7" misread of "Aart") it is the worse source and must not
        # suppress a valid primary reading.
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
        current_relation = record.get("relation_name", "")
        # ``अन्य`` is often a low-confidence fallback for a specific printed
        # father/husband label.  A focused pass with a clean specific label may
        # replace it only when no actual ``अन्य`` person was captured; never
        # overwrite a populated valid relation value.
        relation_is_compatible = (
            not current_relation
            or current_relation == relation
            or (
                current_relation == "अन्य"
                and relation in {"पिता", "पति", "माता"}
                and not record.get("voter_other_name")
            )
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


def _safe_filename(filename: str, default: str = "document.pdf") -> str:
    """Return a filesystem-safe basename for staging and generated output."""
    value = os.path.basename(str(filename or "").replace("\\", "/"))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value.strip(" .")
    if not value:
        value = default
    stem, suffix = os.path.splitext(value)
    if stem.upper().split(".", 1)[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        stem = f"_{stem}"
    max_stem_length = max(1, 180 - len(suffix))
    value = f"{stem[:max_stem_length]}{suffix}"
    return value.rstrip(" .") or default


def _source_pdf_name(filename: str) -> str:
    """Return a safe PDF basename, without client path data."""
    return _safe_filename(filename, "document.pdf")


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
        image_bytes = getattr(page, "get_pixmap")(
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
        image_bytes = getattr(page, "get_pixmap")(
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
    tesseract_house = _clean_house_no(
        _normalize_house_suffix(str(tesseract_house or "")))
    paddle_house = _clean_house_no(
        _normalize_house_suffix(str(metadata.get("house_no", "") or "")))
    confidence = float(metadata.get("house_confidence", 0.0))
    votes = int(metadata.get("house_votes", 0))
    strong_format = bool(metadata.get("house_strong_format", False))
    focused_counts = Counter(
        str(value)
        for value in metadata.get("focused_house_candidates", [])
    )
    focused_prefix = _normalize_house_suffix(
        str(metadata.get("focused_house_prefix", "") or ""))
    if focused_prefix in {"ई-", "इ-"}:
        focused_prefix = "इ-"

    def add_focused_prefix(value: str) -> str:
        if (
            focused_prefix
            and "/" in value
            and not re.match(r"^[^\d\s]+-", value)
        ):
            return focused_prefix + value
        return value

    reasons: list[str] = []

    if tesseract_house and paddle_house == tesseract_house:
        return add_focused_prefix(tesseract_house), "tesseract+paddle", reasons

    paddle_is_slash = bool(re.fullmatch(
        r"\d{1,3}/\d+[A-Za-z\u0900-\u097F]*", paddle_house))
    tesseract_is_slash = "/" in tesseract_house
    if (
        paddle_is_slash
        and focused_counts[paddle_house] >= 2
        and confidence >= 0.70
    ):
        tesseract_match = re.fullmatch(
            r"([^\d]*)(\d+)/(\d+)", tesseract_house)
        paddle_parts = paddle_house.split("/", 1)
        if tesseract_match and paddle_parts[1] == tesseract_match.group(3):
            tess_num = tesseract_match.group(2)
            pad_num = paddle_parts[0]
            safe_num_repair = (
                pad_num == tess_num
                or pad_num == "1" + tess_num
                or (
                    len(pad_num) == len(tess_num)
                    and sum(a != b for a, b in zip(pad_num, tess_num)) == 1
                    and all(
                        a == b or {a, b} in ({"1", "7"}, {"1", "4"})
                        for a, b in zip(pad_num, tess_num)
                    )
                )
            )
            if safe_num_repair:
                return (
                    add_focused_prefix(tesseract_match.group(1) + paddle_house),
                    "focused_tesseract+paddle",
                    reasons,
                )
        if tesseract_is_slash:
            reasons.append("house_ocr_conflict")
            return add_focused_prefix(tesseract_house), "tesseract", reasons
        return paddle_house, "focused_tesseract+paddle", reasons
    # Prefixed slash / empty tesseract fallback. If primary OCR dropped the
    # whole house label, focused prefix OCR may still recover ``इ-``.
    if paddle_is_slash and not tesseract_house and votes >= 2 and confidence >= 0.70:
        prefix = focused_prefix
        return (prefix + paddle_house if prefix else paddle_house), "paddle_fallback", reasons

    if paddle_is_slash and not tesseract_house:
        if confidence >= 0.85 and votes >= 2:
            return paddle_house, "paddle_fallback", reasons
        # Lower threshold for well-supported focused candidates (2+ votes, >=0.70 conf)
        if votes >= 2 and confidence >= 0.70:
            return paddle_house, "paddle_fallback", reasons
        reasons.append("unreliable_paddle_house")
        return "", "missing", reasons

    if paddle_is_slash and tesseract_is_slash:
        paddle_parts = paddle_house.split("/", 1)
        tesseract_match = re.fullmatch(
            r"([^\d]*)(\d+)/(\d+)", tesseract_house)
        if tesseract_match:
            tess_prefix = tesseract_match.group(1)
            tess_num = tesseract_match.group(2)
            tess_denom = tesseract_match.group(3)
            pad_num = paddle_parts[0]
            pad_denom = paddle_parts[1]
            # Case A: Same denominator, numerator repair (leading 1, 1 vs 7, 1 vs 0)
            if pad_denom == tess_denom and confidence >= 0.70:
                pfx = tess_prefix or focused_prefix
                if pad_num == "1" + tess_num:
                    return pfx + paddle_house, "paddle_repair", reasons
                if pad_num == tess_num:
                    return pfx + paddle_house, "paddle_repair", reasons
                if (
                    len(pad_num) == len(tess_num)
                    and sum(l != r for l, r in zip(pad_num, tess_num)) == 1
                    and all(l == r or {l, r} in ({"1", "7"}, {"1", "0"}, {"1", "4"})
                            for l, r in zip(pad_num, tess_num))
                ):
                    return pfx + paddle_house, "paddle_repair", reasons
            # Case B: Same numerator, denominator repair (1 vs 7, confusable digits)
            if (
                pad_num == tess_num
                and confidence >= 0.75
                and votes >= 2
            ):
                if len(pad_denom) == len(tess_denom):
                    diffs = sum(l != r for l, r in zip(pad_denom, tess_denom))
                    if diffs == 1 and all(
                        l == r or {l, r} in ({"1", "7"}, {"1", "4"}, {"0", "8"}, {"3", "8"})
                        for l, r in zip(pad_denom, tess_denom)
                    ):
                        return (tess_prefix or focused_prefix) + paddle_house, "paddle_repair", reasons
                elif pad_denom.startswith(tess_denom) and len(pad_denom) == len(tess_denom) + 1:
                    return (tess_prefix or focused_prefix) + paddle_house, "paddle_repair", reasons
            # Case C: Numerator 0 -> 1 with prefix
            if (
                tess_num == "0"
                and pad_num == "1"
                and pad_denom == tess_denom
                and confidence >= 0.80
                and votes >= 2
            ):
                pfx = tess_prefix or focused_prefix
                return pfx + paddle_house, "paddle_repair", reasons

        # Prefixed house: Tesseract reads EE/E/§/5/8 as a garbled prefix before a dash.
    # Patterns: "8-48" → ई-481, "5-484" → ई-481, "§-0/602" → ई-10/602.
    # Also repair when tesseract has only prefix+digits but misses full value
    # (e.g. 8-48 should become ई-481 when paddle is 481).
    tesseract_prefixed_digits = re.fullmatch(
        r"([58§$EeIiइई]{1,2})-(\d{2,4})", tesseract_house, re.IGNORECASE)
    if tesseract_prefixed_digits:
        norm_pfx = "इ-"
        tesseract_digits = tesseract_prefixed_digits.group(2)
        # Only apply when paddle is pure digits 3-4 chars (not slash-form)
        if re.fullmatch(r"\d{3,4}", paddle_house) and (votes >= 1 or confidence >= 0.75):
            # Extra digit (e.g. 48 -> 481: paddle has 3 digits ending with "48")
            if len(paddle_house) == len(tesseract_digits) + 1 and paddle_house.endswith(tesseract_digits):
                return f"{norm_pfx}{paddle_house}", "paddle_repair", reasons
            # Same digits with prefix
            if len(tesseract_digits) == len(paddle_house) and tesseract_digits == paddle_house:
                return f"{norm_pfx}{paddle_house}", "paddle_repair", reasons
            # Same length, 1-digit difference (e.g. 484 vs 481)
            if len(tesseract_digits) == len(paddle_house):
                diffs = sum(l != r for l, r in zip(tesseract_digits, paddle_house))
                if diffs == 1 and all(
                    l == r or {l, r} in ({"1", "4"}, {"1", "7"}, {"4", "8"}, {"0", "8"})
                    for l, r in zip(tesseract_digits, paddle_house)
                ):
                    return f"{norm_pfx}{paddle_house}", "paddle_repair", reasons

# Plain digits with 1-digit difference (e.g. 7 vs 1, 4 vs 7) — lower votes threshold for focused results.
    if (
        re.fullmatch(r"\d{1,4}", paddle_house)
        and re.fullmatch(r"\d{1,4}", tesseract_house)
        and confidence >= 0.70
    ):
        missing_leading_one = paddle_house == "1" + tesseract_house
        one_confusable_digit = (
            len(paddle_house) == len(tesseract_house)
            and sum(
                left != right
                for left, right in zip(paddle_house, tesseract_house)
            ) == 1
            and all(
                left == right or {left, right} in ({"1", "7"}, {"1", "4"})
                for left, right in zip(paddle_house, tesseract_house)
            )
        )
        independently_supported = strong_format and votes >= 2
        # For single-digit confusions (e.g. 746 → 146 with 1 vote), allow when confidence is high (>=0.75) and votes >= 1.
        high_confidence_single_vote = (one_confusable_digit or missing_leading_one) and confidence >= 0.75 and votes >= 1
        if (
            missing_leading_one
            or one_confusable_digit
            or independently_supported
            or high_confidence_single_vote
        ):
            return paddle_house, "paddle_repair", reasons

    if (
        re.fullmatch(r"\d{1,4}", tesseract_house)
        and re.fullmatch(r"\d{2,5}", paddle_house)
        and paddle_house.endswith("1" + tesseract_house)
        and len(paddle_house) > len(tesseract_house) + 1
        and confidence >= 0.75
        and votes >= 1
    ):
        return "1" + tesseract_house, "paddle_repair", reasons

    if (
        not tesseract_house
        and votes >= 2
        and (
            confidence >= 0.85
            or (strong_format and confidence >= 0.75)
        )
    ):
        return paddle_house, "paddle_fallback", reasons

    structured_clear = False
    # Step 3: Preserve complete structured primary address over numeric artifact.
    # A valid structured address (plot identifiers, plot-no prefixes, plot
    # labels like प्लॉट/प्लाट, multi-part forms with commas/subparts) is
    # higher fidelity than an isolated Paddle numeric reading. Only allow
    # numeric repair when it has existing required vote/confidence support
    # and the primary is clearly malformed (e.g. truncated single-digit).
    structured_address_indicators = {
        "प्लॉट", "प्लाट", "पी.", "प्लॉ", "एचएनओ", "ख", "नं", "पी. नं",
        "प्लॉट नं", "प्लाट नं", "एचएन-O", "ख नं",
    }
    primary_is_structured = any(ind in tesseract_house for ind in structured_address_indicators)
    if primary_is_structured and tesseract_house:
        # Structured primary wins when it contains a structured indicator.
        # Only allow numeric repair if paddle has very high confidence (>=0.85)
        # and 2+ votes, and the structured value is just a single digit or
        # clearly truncated fragment.
        structured_clear = bool(
            (len(tesseract_house.strip()) >= 3 or "/" in tesseract_house)
            and not (tesseract_house.strip().isdigit() and len(tesseract_house) <= 3)
        )
        if structured_clear:
            # Structured primary is reliable — do not replace with numeric.
            if paddle_house and paddle_house != tesseract_house:
                reasons.append("structured_primary_preserved_over_numeric")
            return tesseract_house, "tesseract", reasons
        # If structured value is clearly truncated (e.g. just a digit or very short),
        # allow existing numeric repair rules to proceed below.

    if tesseract_house:
        if (
            paddle_house
            and paddle_house != tesseract_house
            and not (primary_is_structured and structured_clear)
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
    partial_current = current if re.fullmatch(r"\d", current) else ""
    if current and not _is_valid_age(current):
        current = ""
    candidates = [
        (str(value), float(confidence))
        for value, confidence in raw_candidates
        if _is_valid_age(value) and float(confidence) >= 0.55
    ]
    reasons: list[str] = []
    if current and any(value == current for value, _ in candidates):
        if len({value for value, _ in candidates}) > 1:
            reasons.append("age_ocr_conflict")
            return current, "tesseract", reasons
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
    if partial_current:
        partial_matches = [
            value for value, _ in candidates
            if len(value) == 2 and value.startswith(partial_current)
        ]
        if len(partial_matches) >= 2 and len(set(partial_matches)) == 1:
            return partial_matches[0], "paddle_repair", reasons
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
    rendered_images: Optional[tuple[bytes, bytes]] = None,
) -> Optional[dict[str, Any]]:
    card_started = time.perf_counter()
    card_label = f"page={page_number or '?'} card={card_index or '?'}"
    stage_seconds: dict[str, float] = {}

    stage_started = time.perf_counter()
    if rendered_images is None:
        get_pixmap = getattr(page, "get_pixmap")
        hindi_bytes = get_pixmap(
            dpi=200, clip=card_rect, alpha=False).tobytes("png")
        metadata_bytes = get_pixmap(
            dpi=300, clip=card_rect, alpha=False).tobytes("png")
    else:
        hindi_bytes, metadata_bytes = rendered_images
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
        not primary_house_candidate
        or any(marker in primary_house_candidate for marker in ("प्लॉट", "प्लाट", "पी.", "ख "))
    ):
        focused_address = _extract_tesseract_house_address(metadata_bytes)
        if focused_address and not primary_house_candidate:
            primary_house_candidate = focused_address
            record["house_no"] = focused_address
    # Read a printed prefix independently for every slash-form candidate.
    # The engines can agree on ``15/245`` while both omit the short-i ``इ``.
    if "/" in primary_house_candidate or "/" in paddle_house_candidate:
        metadata["focused_house_prefix"] = (
            _extract_tesseract_house_prefix(metadata_bytes))
    if (
        "/" in paddle_house_candidate
        and paddle_house_candidate != primary_house_candidate
        and float(metadata.get("house_confidence", 0.0)) >= 0.70
        and int(metadata.get("house_votes", 0)) >= 2
    ):
        metadata["focused_house_candidates"] = (
            _extract_tesseract_house_candidates(metadata_bytes))
    stage_seconds["paddle_and_house_fallback"] = time.perf_counter() - stage_started

    record["sno"] = _choose_serial_value(
        record.get("sno", ""), metadata.get("sno", ""))
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

    focused_age = ""
    if not _is_valid_age(record.get("age", "")):
        stage_started = time.perf_counter()
        focused_age = _extract_tesseract_age(hindi_bytes)
        stage_seconds["tesseract_age_focused"] = (
            time.perf_counter() - stage_started)
        if _is_valid_age(focused_age):
            record["age"] = focused_age
    age, age_source, age_reasons = _choose_age(
        str(record.get("age") or record.get("_age_ocr_fragment", "")),
        list(metadata.get("age_candidates", [])),
    )
    if focused_age and age_source == "tesseract":
        age_source = "focused_tesseract"
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
    """Probe representative cards so one unreadable row cannot hide a page."""
    card_rects = _voter_card_rects(page)
    probe_indexes = (0, 1, 2, 9, 10, 11, 18, 19, 20, 27, 28, 29)
    hits = 0
    for index in probe_indexes:
        image = getattr(page, "get_pixmap")(
            dpi=200, clip=card_rects[index], alpha=False).tobytes("png")
        if _extract_tesseract_epic(image):
            hits += 1
            if hits >= 1:
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
            render_started = time.perf_counter()
            rendered_cards = (
                _render_page_card_images(page, card_rects)
                if _OCR_FULL_PAGE_RENDER else None
            )
            render_elapsed = time.perf_counter() - render_started
            print(
                f"[OCR] page={page_number} full_page_render="
                f"{render_elapsed:.2f}s mode={'sliced' if rendered_cards else 'fallback'}",
                flush=True,
            )

            def extract_card_at(index_and_rect: tuple[int, fitz.Rect]) -> Optional[dict[str, Any]]:
                index, rect = index_and_rect
                rendered = rendered_cards[index - 1] if rendered_cards else None
                return _extract_card(
                    page, rect, page_number, index, rendered)

            indexed_rects = list(enumerate(card_rects, 1))
            worker_count = _OCR_CARD_WORKERS
            if worker_count > 1:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    extracted_records = list(executor.map(
                        extract_card_at, indexed_rects))
            else:
                extracted_records = [extract_card_at(item) for item in indexed_rects]
            for (card_index, _), record in zip(indexed_rects, extracted_records):
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
            allowed_staged_path = (
                source_path == BATCH_WORK_DIR
                or source_path.is_relative_to(BATCH_WORK_DIR)
            )
            if not allowed_staged_path and not _path_is_allowed(source_path):
                raise ValueError("source path is outside OCR_BATCH_ALLOWED_ROOTS")
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
) -> Response:
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
        result["json_output_file"] = output_path.name
        await asyncio.to_thread(_write_json_atomically, output_path, result)
        return Response(
            content=json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
        )
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
        cast(Any, process_pdf_batch_task).apply_async(
            args=[payload], task_id=job_id,
        )
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
    except Exception:
        shutil.rmtree(input_dir.parent, ignore_errors=True)
        raise
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
        celery_result = cast(Any, AsyncResult)(job_id, app=cast(Any, celery_app))
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

    def check(condition: Any, label: str) -> None:
        nonlocal checks
        checks += 1
        if not bool(condition):
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

    compact_father = parse_voter_box_from_ocr_lines([
        "नाम: सपना",
        "पिता कानाम: इंद्र पाल",
        "मकान संख्या: हाऊस नं 15/245",
        "फोटो उपलब्ध है",
        "आयु: 21 लिंग: महिला",
    ])
    check(
        compact_father["relation_name"] == "पिता"
        and compact_father["voter_father_name"] == "इंद्र पाल"
        and compact_father["house_no"] == "15/245",
        "compact father label and house",
    )
    compact_husband = parse_voter_box_from_ocr_lines([
        "नाम: प्रियंका जोशी",
        "पति का नाम: धर्मेंद्र",
        "फोटो उपलब्ध है",
        "मकान संख्या: हाऊस नं 121",
    ])
    check(
        compact_husband["relation_name"] == "पति"
        and compact_husband["voter_husband_name"] == "धर्मेंद्र"
        and "फोटो" not in str(compact_husband),
        "husband relation and photo guard",
    )

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
    other_relation = parse_voter_box_from_ocr_lines(["अन्य: सुनीता"])
    check(
        other_relation["relation_name"] == "अन्य"
        and other_relation["voter_other_name"] == "सुनीता",
        "bare other relation label",
    )
    lone_one = parse_voter_box_from_ocr_lines([
        "नाम: ममता देवी",
        "मकान संख्या : | फोटो उपलब्ध है",
        "आयु: 50 लिंग: महिला",
    ])
    check(lone_one["house_no"] == "1", "lone printed one")
    garbled_house_label = parse_voter_box_from_ocr_lines([
        "नाम: सीमा",
        "भकान संख्या : 4 फोटो उपलब्ध है",
    ])
    check(garbled_house_label["house_no"] == "4", "garbled house label")
    garbled_house_count = parse_voter_box_from_ocr_lines([
        "नाम: सीमा",
        "मकान संखा : 444 फोटो उपलब्ध है",
    ])
    check(garbled_house_count["house_no"] == "444", "garbled house count")
    garbled_deat_label = parse_voter_box_from_ocr_lines([
        "नाम: सीमा",
        "मकान deat: 7 भी फोटो उपलब्ध है",
    ])
    check(garbled_deat_label["house_no"] == "7 बी", "garbled deat label")
    comma_house = parse_voter_box_from_ocr_lines([
        "नाम: सीमा",
        "मकान संख्या : इ-8,496 फोटो उपलब्ध है",
    ])
    check(comma_house["house_no"] == "इ-8,496", "comma house suffix")
    plot_house = parse_voter_box_from_ocr_lines([
        "नाम: सीमा",
        "मकान संख्या : प्लॉट नं 279 ख नं 79 फोटो उपलब्ध है",
    ])
    check(
        plot_house["house_no"] == "प्लॉट नं 279 ख नं 79",
        "plot house details",
    )
    plot_address = parse_voter_box_from_ocr_lines([
        "नाम: सीमा",
        "मकान संख्या : पी. नं-बि 90, ख 4-707 फोटो उपलब्ध है",
    ])
    check(
        plot_address["house_no"] == "पी. नं-बि 90, ख 4-707",
        "preserve plot address",
    )
    gender_misread = parse_voter_box_from_ocr_lines([
        "आयु : 28 लिंग : घुरुष",
    ])
    check(gender_misread["gender"] == "पुरुष", "common male OCR variant")
    devanagari_age = parse_voter_box_from_ocr_lines([
        "नाम: सीमा", "आयु: २७ लिंग: महिला",
    ])
    check(devanagari_age["age"] == "27", "Devanagari age")
    partial_age = parse_voter_box_from_ocr_lines(["आयु : 2 लिंग : महिला"])
    check(partial_age["_age_ocr_fragment"] == "2", "retain partial age OCR")
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

    check(_choose_serial_value("525", "") == "525", "Tesseract serial fallback")
    check(_choose_serial_value("525", "526") == "526", "Paddle serial preference")

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
    shorter_slash = _choose_house_number(
        "15/245", {
            **house_meta("5/245", 0.90, 2),
            "focused_house_candidates": ["5/245", "5/245"],
        },
    )
    check(
        shorter_slash[0] == "15/245"
        and shorter_slash[1] == "tesseract"
        and "house_ocr_conflict" in shorter_slash[2],
        "preserve valid slash numerator",
    )
    check(
        _choose_house_number(
            "ई-0/602", house_meta("10/602", 0.90, 2))[0] == "इ-10/602",
        "normalize short-i house prefix",
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
        _choose_house_number("ई-0/602", prefixed_house)[0] == "इ-10/602",
        "focused house consensus preserves short-i prefix",
    )
    check(
        _choose_house_number(
            "8/8", house_meta("8/81", 0.846, 2))[0] == "8/81",
        "repair truncated slash denominator",
    )
    denominator_prefix = house_meta("1/81", 0.90, 2)
    denominator_prefix["focused_house_prefix"] = "इ-"
    check(
        _choose_house_number("1/8", denominator_prefix)[0] == "इ-1/81",
        "preserve focused prefix during denominator repair",
    )
    check(
        _choose_house_number(
            "02", house_meta("4102", 0.938, 1))[0] == "102",
        "repair noisy leading house digits",
    )
    check(
        _choose_house_number(
            "5-484", house_meta("481", 0.848, 2))[0] == "इ-481",
        "repair OCR prefix and final digit",
    )
    leading_slash = house_meta("1/75", 0.863, 2)
    leading_slash["focused_house_prefix"] = "इ-"
    check(
        _choose_house_number("0/75", leading_slash)[0] == "इ-1/75",
        "repair slash leading one and prefix",
    )

    # Structured-address arbitration fixtures (Step 3 / regression)
    check(
        _choose_house_number(
            "पी. नं-बि 90, ख 4-707",
            {"house_no":"146","house_confidence":0.789,"house_votes":1},
        )[0] == "पी. नं-बि 90, ख 4-707" and
        _choose_house_number("पी. नं-बि 90, ख 4-707", {"house_no":"146","house_confidence":0.789,"house_votes":1})[1] == "tesseract",
        "structured plot preserved over numeric paddle",
    )
    check(
        _choose_house_number(
            "279 ख नं 79",
            {"house_no":"746","house_confidence":0.789,"house_votes":1},
        )[0] == "279 ख नं 79",
        "structured plot kh preserved over numeric",
    )
    check(
        _choose_house_number(
            "449/9",
            {"house_no":"", "house_confidence":0.0, "house_votes":0},
        )[0] == "449/9",
        "structured slash kept when paddle missing",
    )

    check(_choose_age("9", [("19", 0.90)])[0] == "19", "age leading one")
    check(_choose_age("47", [("41", 0.90)])[0] == "47", "age conflict")
    check(_choose_age("", [("41", 0.90), ("41", 0.80)])[0] == "41",
          "unanimous age fallback")
    check(_choose_age("", [("41", 0.90), ("47", 0.90)])[0] == "",
          "ambiguous age fallback")
    check(
        _choose_age(
            "2", [("21", 0.69), ("21", 0.68),
                  ("34", 0.69), ("34", 0.68)]
        )[0] == "21",
        "partial age corroborated by Paddle",
    )

    import numpy as np

    blank_card = np.zeros((100, 200, 3), dtype=np.uint8)
    masked_card = _mask_card_noise_for_tesseract(blank_card)
    check(np.all(masked_card[:, :8] == 255), "mask left card border")
    check(np.all(masked_card[95:, :] == 255), "mask bottom card border")
    check(
        np.all(masked_card[30:90, 160:195] == 255),
        "mask photo box and label",
    )
    check(
        _preprocess_tesseract_card_image(
            blank_card, mask_photo_box=False) is blank_card,
        "serial crop can opt out of photo mask",
    )

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

    # Focused crops can include a one-token tail from the photo/next-card
    # region. Keep the complete primary name instead of exposing that tail.
    primary_name = parse_voter_box_from_ocr_lines(["नाम: शिवानी देवी"])
    focused_name = parse_voter_box_from_ocr_lines([
        "नाम: शिवानी देवी हु",
        "पति का नाम: आशीष कुमार",
    ])
    _merge_focused_name_and_relation(primary_name, focused_name)
    check(
        primary_name["voter_first_name"] == "शिवानी"
        and primary_name["voter_middle_name"] == ""
        and primary_name["voter_sur_name"] == "देवी"
        and primary_name["voter_husband_name"] == "आशीष कुमार",
        "reject focused stray name tail",
    )

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