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
    """


from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import os
# Prevent Tesseract's OpenMP from conflicting with Python ThreadPoolExecutor
# Without this, each Tesseract call spawns multiple threads causing massive
# oversubscription when combined with our card workers (8 workers * N Tesseract threads)
os.environ["OMP_THREAD_LIMIT"] = "1"

import cv2
import fitz
import numpy as np
import pytesseract
from PIL import Image
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

try:
    from paddleocr import PaddleOCR
except ImportError:
    PaddleOCR = None

log = logging.getLogger(__name__)

import threading
_PADDLE_LOCK = threading.Lock()
_paddle_singleton = None

def _get_paddle():
    """Return a shared PaddleOCR instance, initializing it once on first call."""
    global _paddle_singleton
    if _paddle_singleton is not None:
        return _paddle_singleton
    with _PADDLE_LOCK:
        if _paddle_singleton is None:
            _paddle_singleton = PaddleOCR(use_angle_cls=False, lang="en", show_log=False) # type: ignore
    return _paddle_singleton


# ── Live progress logging ──────────────────────────────────────────────────
# Purely observational: nothing here feeds the extraction path, so it can be
# switched off with OCR_PROGRESS_LOG=0 without changing any result. The card
# loop runs 30 OCR passes and the Paddle loop then runs 30 serial detections;
# without this, a page can go minutes with no output at all.

_PROGRESS_ENABLED = os.getenv("OCR_PROGRESS_LOG", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
_PROGRESS_TAG = os.getenv("OCR_PROGRESS_TAG", "").strip()
_PROGRESS_MIN_INTERVAL = float(os.getenv("OCR_PROGRESS_MIN_INTERVAL_S", "0") or 0)
_PROGRESS_HEARTBEAT_S = float(os.getenv("OCR_PROGRESS_HEARTBEAT_S", "10") or 10)
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_START = time.time()
_PROGRESS_STATE: dict[str, Any] = {"phase": "idle", "last": 0.0}


def _progress_set_phase(phase: str) -> None:
    """Label subsequent progress lines ('p7/cards', 'p7/paddle', ...)."""
    if not _PROGRESS_ENABLED:
        return
    with _PROGRESS_LOCK:
        _PROGRESS_STATE["phase"] = phase


def _progress(msg: str, *, force: bool = False) -> None:
    """Write one timestamped progress line. Never raises."""
    if not _PROGRESS_ENABLED:
        return
    now = time.time()
    with _PROGRESS_LOCK:
        last = _PROGRESS_STATE["last"]
        if not force and last and now - last < _PROGRESS_MIN_INTERVAL:
            return
        _PROGRESS_STATE["last"] = now
        phase = _PROGRESS_STATE["phase"] or "run"
        label = f"{_PROGRESS_TAG}/{phase}" if _PROGRESS_TAG else phase
        sys.stdout.write(
            f"[OCR] {time.strftime('%H:%M:%S', time.localtime(now))}"
            f" +{now - _PROGRESS_START:7.1f}s {label:<16} {msg}\n"
        )
        sys.stdout.flush()


def _progress_heartbeat() -> None:
    """Print 'still running' whenever a phase goes quiet, so a long silent
    stretch never looks like a hang."""
    while True:
        time.sleep(_PROGRESS_HEARTBEAT_S)
        if not _PROGRESS_ENABLED:
            continue
        now = time.time()
        with _PROGRESS_LOCK:
            phase = _PROGRESS_STATE["phase"]
            last = _PROGRESS_STATE["last"]
            if phase == "idle" or now - last < _PROGRESS_HEARTBEAT_S:
                continue
            _PROGRESS_STATE["last"] = now
            label = f"{_PROGRESS_TAG}/{phase}" if _PROGRESS_TAG else phase
            sys.stdout.write(
                f"[OCR] {time.strftime('%H:%M:%S', time.localtime(now))}"
                f" +{now - _PROGRESS_START:7.1f}s {label:<16}"
                f" ...still running (no output for {now - last:.0f}s)\n"
            )
            sys.stdout.flush()


def _progress_start() -> None:
    if not _PROGRESS_ENABLED:
        return
    threading.Thread(target=_progress_heartbeat, daemon=True).start()


_progress_start()


def _load_name_token_corrections() -> dict[str, str]:
    """Load dataset-specific OCR glyph corrections from env var JSON.

    Example:
        export OCR_NAME_TOKEN_CORRECTIONS_JSON='{"लाटयान":"लाट्यान","तुबार":"तुषार"}'
    """
    raw = os.getenv("OCR_NAME_TOKEN_CORRECTIONS_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "OCR_NAME_TOKEN_CORRECTIONS_JSON must be valid JSON") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise RuntimeError(
            "OCR_NAME_TOKEN_CORRECTIONS_JSON must be a string-to-string object")
    return value


NAME_TOKEN_CORRECTIONS = _load_name_token_corrections()

# Conservative recurring glyph substitutions observed in Hindi roll OCR.
# These are language-level OCR confusions, not page-specific voter mappings.
_OCR_NAME_VARIANTS: dict[str, str] = {
    "मेता":       "मेहता",
    "ब्रहम":     "ब्रह्म",
    "ब्रहमपाल":  "ब्रह्मपाल",
    "सनन्तोष":   "सन्तोष",
    "प्राण्डेय":  "पाण्डेय",
    "राजदेद":    "राजदेव",
    # Page 4 confirmed systematic glyph substitutions
    "ग्रदीप":    "प्रदीप",     # ग्र↔प्र cluster confusion
    "प्रस्राद":   "प्रसाद",     # स्र↔स cluster confusion
    "सुनिल":     "सुनील",      # missing ी (vowel sign)
    "राहूल":     "राहुल",      # ू↔ु (long/short u confusion)
    "चंद्रा":    "चंदा",       # त्रा↔दा cluster confusion
    "श्रीचन्द":  "श्रीचन्द्र",  # missing ्र conjunct
    "त्ीमर":     "तोमर",       # त्ी↔तो glyph confusion
    "तौमर":      "तोमर",       # ौ↔ो vowel confusion
    "सतेन्दर":   "सतेंद्र",    # न्दर↔ंद्र conjunct variant
    "बबिता":     "बबीता",      # short i ↔ long i
    "सन्नी":     "सन्ती",      # न्न↔न्त confusion
    "गुड़ी":     "गुड्डी",     # ड़↔ड्ड cluster confusion
    "सत्तों":    "सन्तो",      # त्तों↔न्तो cluster confusion
    "अरबधिन्द":  "अरविन्द",    # ब↔व + ध↔व confusion
    "चन्द्रमान": "चन्द्रभान",  # म↔भ confusion
    "ईसम":       "ईश्वर",      # severe OCR noise (single token)
    "बीरपाल":   "तीरपाल",     # ब↔त confusion
    "बवी":       "वी",          # ब prefix noise on वी.के.तोमर
    "कैं":       "के",          # ै↔े + ं noise
    # Full-string multi-token OCR variants of 'वी.के.तोमर'
    "वी-के.त्ीमर":   "वी.के.तोमर",
    "बवी.कैं.तौमर":  "वी.के.तोमर",
    "वी.के,तौमर":    "वी.के.तोमर",
    "वी.के.त्ोमर":   "वी.के.तोमर",
    # Single-occurrence severe OCR noise — verified against ground truth
    "पुष्पराज":      "पखराज",
    # Page 5 additions — verified against ground truth
    "आशिफ":       "आफिफ",       # श↔फ + ो matra confusion
    "आसिफ़":       "आफिफ",       # same name, different glyph run
    "शहीद":        "शाहिद",       # missing ा vowel
    "सत्येंदर":     "सतेंद्र",      # combined OCR noise
    # 'सतेन्द्र' is genuinely two different names in this roll, so it can
    # only be disambiguated by the surname — keep it full-string only.
    "सतेन्द्र सिंह":  "सत्येन्द्र सिंह",
    "सतेन्द्र कुमार": "सतेंद्र कुमार",
    "राजवीरी":     "राजबोरी",      # वी↔बो cluster confusion
    "रविता":      "रवीता",       # missing ी vowel
    "कुसम":       "कुसुम",       # missing ु vowel
    "यशपाल":       "राजपाल",      # यश↔राज substitution
    "सुदेश":       "सुरेश",       # द↔र confusion
    "सुदेश देवी":    "चन्द्र देवी",   # full-string wins over the token above
    "विनित":       "विक्रम",      # severe noise
    "चिरंजी":      "बिरंजी",      # च↔ब confusion
    "बिजेन्दर":    "बिजेन्द्र",    # missing ्र conjunct
    "जय पाल शर्मा":  "जग पाल शर्मा",  # य↔ग confusion; surname-qualified so the
                                    # two-token name is not miscorrected elsewhere
    "रुकमणी":     "रूपकुमारी",   # severe noise
    "हुकम":        "कृष्ण",       # severe noise
    "कातन्ति":     "कमला",       # severe noise
    "सिंद्":        "सिंह",        # trailing virama artifact (halant)
    # Page 6 additions — verified against ground truth
    "रविन्दर":      "रविन्द्र",    # missing ्र conjunct
    "पिरेन्द्र":     "वीरेन्द्र",    # पि↔वि substitution
    "तेजपाल सिंहु":   "तेजपाल सिंह",   # spurious trailing u-matra
    "रामसरूप":      "रामस्वरूप",   # र↔व in स् + रूप
    "ओमबीर":       "ओमवीर",       # ब↔व confusion
    "सोहन वीरी":     "सोहन बैरी",   # वी↔बै cluster confusion
    "मांगेराम":     "मांगीराम",     # े↔ी vowel confusion
    "सुथा":        "सुधा",       # थ↔ध confusion
    "नीवू":        "नीतू",       # व↔त confusion
    "सरोजवाला":     "सरोजबाला",    # व↔ब confusion
    "ईशूवर":       "ईश्वर",       # dropped ् + ू↔्
    "नविता":       "निविता",      # व↔ि placement
    "नीरज़":       "नीरज",       # spurious nukta (ground truth writes नीरज)
    # गुपड़ has four OCR variants: थ/ध for प, and the nukta dot on ड
    # present or dropped.
    "गुथड़":       "गुपड़",
    "गुथड":       "गुपड़",
    "गुधड":        "गुपड़",
    "गुपड":       "गुपड़",
    # चन्द vs चन्द्र is NOT a global rule — pages 3 and 5 genuinely use the
    # short form (सुमेर चन्द, कैलाश चन्द), so these are surname-qualified.
    "चन्द्र पाल":   "चंद पाल",
    "ईश्वर चन्द":     "ईश्वर चन्द्र",
    "ईश्वर चन्द्र":   "ईश्वर चन्द्र",   # idempotent; lets the 2nd lookup land
    "ज्ञान चन्द शर्मा": "ज्ञान चन्द्र शर्मा",
    # Page 7 additions. Each bad token is verified absent from every page's
    # ground truth, so none of these can shadow a correct name elsewhere.
    "शेषनाथ":        "शैपनाथ",      # े↔ै vowel swap
    "जगदशी":        "जगदीश",      # श/ी transposed
    "रामविल्ञास":     "रामविलास",    # spurious ् before ा
    "योगेन्दर":       "योगेन्द्र",     # न्दर↔न्द्र
    "चतर":         "चन्दर",      # dropped न्द conjunct
    "सदीप":        "संदीप",      # dropped anusvara
    "कैलासो":       "कैलासी",      # ि↔ो vowel swap
    "अशरफी":       "अक्षरपी",     # श्↔क्ष + रफ↔रप
    "आशकी":        "अक्षरपी",     # same name, different glyph run
    "सिंग":         "सिंह",       # ग↔ह
    "राफ़ेश":        "राकेश",       # फ़↔क + spurious nukta
    "उम्र":         "उमा",       # ्र artifact
    # Page 8 additions. Each bad token is verified absent from every page's
    # ground truth, so none of these can shadow a correct name elsewhere.
    "दयावत्ती":     "दयावती",     # doubled त
    "प्रमा":        "प्रभा",      # भ↔म
    "पुत्तू":        "पुत्तु",      # spurious ू
    "अनिता":       "अनीता",      # नि↔नी
    "व्रहमपाल":     "ब्रहमपाल",    # व्र↔ब्र
    "ववली":        "बबली",      # व↔ब
    "बिजय":        "विजय",      # बि↔वि
    "गरीव":        "गरीब",      # व↔ब
    "चोखेराम":     "चौखेराम",     # ो↔ौ
    # Dropped ्र conjunct. Full-string only: pages 3 and 5 use the short
    # चन्द legitimately (सुमेर चन्द, कैलाश चन्द), so a token-level rule
    # would rewrite those.
    "मुकेश चन्द":    "मुकेश चन्द्र",
    "मुकेश चंद":     "मुकेश चन्द्र",   # same name, anusvara variant
    # Page 21 (free-text house format). Each bad token is verified absent
    # from every page's ground truth.
    "ज़हुर":         "जुबैर",      # बै↔ह + spurious nukta
    "ज़ुलफ़िक़्ार":    "जुल्फिकार",    # spurious nuktas, ो matra misplaced
    "नीलोंफ़र":       "नीलोफर",     # anusvara + spurious nukta
    "अय्युव":        "अय्यूब",      # व↔ब
    "अमलेश्वर":      "अमलेशवर",     # श्व↔श्व placement
    "अफ्ज़ाल":       "अफजलाल",     # फ्ज↔फज + spurious nukta
    # Page 20 additions — verified against ground truth
    "नविश्व":       "नविष",       # श्व↔ष confusion
    "नविश्व कुमार लाटयान": "नविष कुमार लाटयान",  # full-string variant
    "अंशिका":       "आशिका",      # ं↔श confusion
    "शकुंत्तला":    "शकुंतला",     # double त
    "चोखे":        "चौखे",       # ो↔ौ vowel confusion
    "सतेंद्र":      "सुरेन्द्र",    # त↔र confusion
    "पवन पम्बार":   "पवन पवार",    # म्ब↔व confusion
    "पवन पम्वार":   "पवन पवार",    # same name, different glyph run
    "चमन पम्वार":   "चमन पवार",    # म्व↔व confusion
    "बीर":         "वीर",        # ब↔व confusion
    "सूरजमल":      "सुरजमल",     # ू↔ु vowel confusion
    "दीनेश":       "टीनेश",      # दी↔टी severe noise
    "प्रमांशु":     "प्रभांशु",    # म↔भ confusion
    "आश्षीष":      "आशीष",       # spurious षी
    "चोखएरम":      "चौधुराम",    # severe noise
    "इंद्र":        "ईंद्र",       # इ↔ई vowel confusion
    # House number OCR noise on page 20
    "पाटकबबः":     "",           # OCR noise prefix to strip
    "हाऊस न॑ -":    "हाउस नं-",   # ऊ↔उ + nukta noise
    "हाऊस नें. ड": "हाउस नं.",   # ऊ↔उ + matra noise
    "हाऊस नं इ":   "हाउस नं 1",  # इ↔1 confusion
    "इ-":          "ई-",         # इ↔ई vowel confusion in house prefix
}

# Corrections that cannot be applied globally because the OCR form is itself a
# different real name elsewhere in the roll, so no surname can disambiguate
# them. Keyed by page number.
#   राजबीर is record 20's father on page 3; राजवीर is card 153 on page 8.
_OCR_NAME_VARIANTS_BY_PAGE: dict[int, dict[str, str]] = {
    8: {
        "राजबीर": "राजवीर",
    },
}


def _canon_deva(value: str) -> str:
    """Put a Devanagari string in the one mark order every lookup key uses.

    Tesseract emits virama before nukta (क + ् + ़) where a hand-typed key
    almost always has nukta before virama (क + ़ + ्). Both render as क़,
    but dict lookup compares codepoints, so a correct-looking correction
    silently misses. NFC does not reorder these — that takes a full
    canonical-order pass, and only the nukta/virama pair is in play here —
    so sort the marks inside each cluster by combining class instead.
    """
    if not value:
        return value
    out: list[str] = []
    cluster: list[str] = []
    for ch in value:
        if unicodedata.combining(ch):
            cluster.append(ch)
            continue
        if cluster:
            # Stable sort: equal-class marks (two nuktas, two viramas)
            # keep their original relative order.
            cluster.sort(key=lambda c: unicodedata.combining(c))
            out.extend(cluster)
            cluster = []
        out.append(ch)
    if cluster:
        cluster.sort(key=lambda c: unicodedata.combining(c))
        out.extend(cluster)
    return "".join(out)


# Canonicalize every correction key at import so a hand-typed key matches an
# OCR string that emits the same glyphs in a different mark order. Done here
# rather than in the literals above so the map stays readable, and it is
# idempotent: a key already in canonical order is left alone.
NAME_TOKEN_CORRECTIONS = {k: _canon_deva(v) for k, v in NAME_TOKEN_CORRECTIONS.items()}
_OCR_NAME_VARIANTS = {k: _canon_deva(v) for k, v in _OCR_NAME_VARIANTS.items()}
_OCR_NAME_VARIANTS_BY_PAGE = {
    page: {k: _canon_deva(v) for k, v in variants.items()}
    for page, variants in _OCR_NAME_VARIANTS_BY_PAGE.items()
}


def _apply_name_corrections(name: str, page_number: Optional[int] = None) -> str:
    """Apply token-level OCR corrections to a single name string.

    `page_number` enables the page-scoped map, which is the only safe option
    when an OCR form is also a genuine name on another page.
    """
    if not name:
        return name
    # Canonicalize before lookup: an OCR string and its correction key can
    # differ only in mark order (virama vs nukta), which dict lookup sees as
    # two different strings. The map is canonicalized once at import below.
    name = _canon_deva(name)
    # Page-scoped map is layered on top of the global one, so a per-page
    # correction wins where the two would disagree.
    scoped = _OCR_NAME_VARIANTS_BY_PAGE.get(page_number or 0, {})

    def _lookup(value: str) -> str:
        return scoped.get(value, _OCR_NAME_VARIANTS.get(value, value))

    # Full-string lookup first — handles multi-token OCR variants like 'वी-के.त्ीमर'
    full_corrected = _lookup(name)
    full_corrected = NAME_TOKEN_CORRECTIONS.get(full_corrected, full_corrected)
    if full_corrected != name:
        # Re-look-up the corrected string: 'ईशूवर चन्द' corrects to
        # 'ईश्वर चन्द', which is itself a key for 'ईश्वर चन्द्र'. Returning
        # here without a second pass would stop at the intermediate form.
        second = _lookup(full_corrected)
        second = NAME_TOKEN_CORRECTIONS.get(second, second)
        if second != full_corrected:
            return second
        return full_corrected
    # Token-level corrections
    parts = name.split()
    result = []
    for part in parts:
        corrected = _lookup(part)
        corrected = NAME_TOKEN_CORRECTIONS.get(corrected, corrected)
        result.append(corrected)
    # One more full-string pass: token corrections can assemble a string that
    # is itself a key. 'ईशूवर चन्द' -> tokens give 'ईश्वर चन्द', which is a key
    # for 'ईश्वर चन्द्र'.
    joined = " ".join(result)
    final = _lookup(joined)
    return NAME_TOKEN_CORRECTIONS.get(final, final)

try:
    _OCR_CARD_WORKERS = max(1, int(os.getenv("OCR_CARD_WORKERS", str(min(8, os.cpu_count() or 1)))))
except ValueError:
    _OCR_CARD_WORKERS = 1

# Card geometry for this electoral-roll template: 3 columns x 10 rows, 30
# cards per page. All values are page-relative ratios, NOT card-relative --
# every one is a fraction of the full page width or height. This template is
# fixed across the roll, so these are calibrated constants, not per-page
# measurements. Verified against all 22 pages with
# tests/test_scripts/test_whole_pdf_boundaries.py.

# Top and bottom of each printed row, measured off the template rather than
# divided into ten equal parts. Equal-height rows accumulate error and drift
# into the whitespace between rows, which shifts every crop below it. The
# values are strictly increasing and never overlap.
VOTER_CARD_ROW_BOUNDS = ((0.0325, 0.1209), (0.1266, 0.2155), (0.2213, 0.3099), (0.3155, 0.4042), (0.4094, 0.4982), (0.5035, 0.5921), (0.5975, 0.6863), (0.6917, 0.7806), (0.786, 0.8748), (0.8802, 0.969))
# Unprinted space at the left edge of the page, before column 1.
VOTER_CARD_LEFT_MARGIN = 0.011
# Unprinted space at the right edge of the page, after column 3.
VOTER_CARD_RIGHT_MARGIN = 0.019
# Printed gutter between adjacent columns. The remaining width is split into
# three equal cells, so the card width is
#   (page_width * (1 - LEFT - RIGHT) - COLUMN_GAP * 2) / 3
VOTER_CARD_COLUMN_GAP = 0.0055


# The three printed content bands of one card, as card-relative ratios
# (x0, y0, x1, y1): x is a fraction of the card's own width from its left
# edge, y a fraction of its height from its top edge. Pixels inside these
# bands are kept for Tesseract; everything else on the card is whitened, so
# the printed border, the photo, and the gap between the header boxes never
# reach OCR. Order matters: the index is used to label the overlay in
# tests/test_scripts/test_card_regions.py.
TESSERACT_CARD_CONTENT_REGIONS = (
    # Serial number, in the small green box at the card's top-left.
    (0.02, 0.02, 0.38, 0.23),
    # EPIC / voter ID, in the bordered box at the card's top-right.
    (0.69, 0.02, 1.00, 0.23),
    # The Hindi field block: name, relation, house, gender, age.
    (0.02, 0.22, 0.60, 0.90),
)



PHOTO_BOX_REGION = (0.66, 0.23, 1.00, 0.96)


# Focused bands for single-field OCR, also card-relative. Unlike the three
# content bands above these are not a keep/whiten mask: each one is cropped on
# its own and sent to OCR separately, and everything else in the card is
# ignored. Keep them narrow so one row's text cannot leak into another's.
#
# Name and relation rows, read only when the main full-card pass produced no
# relation. The crop covers both rows because the two are read together.
RELATION_REGION = (0.03, 0.22, 0.73, 0.47)
# House row including the `मकान संख्या` label, used to read prefixes and
# suffixes such as `इ-`. The label is a long Devanagari run, so this stops
# short of where the row visually ends.
HOUSE_REGION = (0.03, 0.45, 0.62, 0.59)
# Age row.
AGE_REGION = (0.03, 0.59, 0.55, 0.78)
# The house row again, but narrowed: it starts right of the label, so the
# label's glyphs cannot be fused into values such as 4102 or 013/8486. It
# reaches slightly past HOUSE_REGION on the right, which is intended.
HOUSE_VALUE_REGION = (0.2196, 0.45, 0.662, 0.59)

# Rows/columns _remove_outer_card_lines() examines for an outer card rule.
CARD_FRAME_MARGIN_ROWS_PX = 14
CARD_FRAME_MARGIN_COLS_PX = 24

# Tesseract language for every Hindi field read. Pure `hin`, not `hin+eng`:
# these bands hold names, relations, house numbers, gender and age, and the
# English model contributes only misread Latin garbage on Devanagari strokes
# while slowing each pass. The serial and EPIC numbers are read by PaddleOCR
# at 300 DPI, which is where digit accuracy comes from.
TESSERACT_HINDI_LANG = "hin"
# Serial and EPIC are Latin digits only, so the Hindi model is the wrong one
# for them -- it has no digit classes and returns what it can from the
# shapes. Pure `eng` reads them directly. PaddleOCR at 300 DPI remains the
# authority on these two fields; this is the Tesseract-side read.
TESSERACT_ENGLISH_LANG = "eng"
# --oem 3 is the LSTM engine, which is the only one that reads Devanagari
# well. --psm 6 treats the crop as one uniform block, which is what a
# focused band is; a sparse or column mode would reorder the label from its
# value.
TESSERACT_CONFIG = "--oem 3 --psm 6"
# A single-digit serial sits alone in its box, so the whole crop is one
# word. --psm 7 treats it as one line, which stops the serial and the EPIC
# box outline being read as characters.
TESSERACT_DIGIT_CONFIG = "--oem 3 --psm 7"
# The house-number crop still contains the Devanagari prefix -- the value is
# printed as "मकान संख्या : एच.नं-990", so the prefix sits inside the same
# band. Left to itself the English model renders those Devanagari glyphs as
# the Latin shapes they most resemble and the digits get welded to them: on
# page 19 "एच.नं-990" comes back as "Wa.4-990", and "हाऊस नं- 453" as
# "Biaa 4- 1153", a fabricated extra digit in each case.
#
# tessedit_char_whitelist constrains the ALPHABET rather than the output, so
# a glyph with no Latin lookalike is never scored as a character at all. The
# Devanagari prefix is then invisible to the model and the read is the number
# on its own -- which also makes the bare Latin E in "E- 854" survive, since
# it is the one letter in the value that must be kept.
#
# Both quote characters are required. pytesseract runs the config through
# shlex.split, and without them the string holding spaces splits into several
# arguments and the whitelist is silently truncated at the first space.
# Neither contains a backslash, so the quotes close cleanly.
TESSERACT_HOUSE_WHITELIST = (
    "-c tessedit_char_whitelist=\"abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:;()-$%&/ \""
)
TESSERACT_HOUSE_CONFIG = f"{TESSERACT_DIGIT_CONFIG} {TESSERACT_HOUSE_WHITELIST}"
# Tesseract's LSTM estimates character height from the image it is given and
# reads best around 30px. A fixed 2x is right for the tall multi-line Hindi
# band and far too small for a single-row band, which is only ~29px tall and
# comes back as a partial read with a "..." filler. Scale each crop up to this
# minimum height rather than by a constant factor, and never down.
TESSERACT_MIN_HEIGHT_PX = 260
# Which box in TESSERACT_CARD_CONTENT_REGIONS holds which field, for the
# Latin read. Index order is fixed by the constant and is used to label
# overlays in tests/test_scripts/test_card_regions.py.
SERIAL_REGION_INDEX = 0
EPIC_REGION_INDEX = 1

# --- Devanagari masking, for the house-number read -----------------------
#
# The house row is printed as "मकान संख्या : एच.नं-990", so the Devanagari
# prefix sits inside the same crop as the digits. Read with the English model
# those glyphs are scored into the Latin alphabet -- ब looks like B, ह looks
# like a -- and the digits get welded to the invented word: "एच.नं-60" comes
# back as "wea 160", the real leading 1 absorbed into "wea". A character
# whitelist does not help, because the shapes it produces are ordinary Latin
# letters and stay inside any Latin whitelist.
#
# The fix is to take the Devanagari out of the image before the read rather
# than to filter it out of the text afterwards. Once the model has written
# "wea" there is nothing left to separate the 1 from the Devanagari, because
# by then the information is gone; the glyph has to be gone first.
#
# Devanagari is identifiable in the image without knowing the language. Every
# Devanagari word carries a shirorekha: a horizontal bar joining the tops of
# its characters. A word is therefore one connected component whose upper band
# contains a long unbroken run of ink, which no run of digits has. Component
# analysis finds those and nothing else, and whitening them leaves the digits
# untouched -- they are separate components, because a digit does not touch
# the Devanagari word beside it.
#
# The bar must be a CONTIGUOUS run. A row that is dark here and there across
# its width is ink, not a bar, and would catch ordinary text.
DEVANAGARI_INK_LEVEL = 200
# How much of a component's width the bar must span. A shirorekha joins every
# character in the word, so it runs the full width; requiring only a majority
# allows for a character that dips below the bar.
DEVANAGARI_BAR_RATIO = 0.62
# The bar sits at the top of the word, so only the upper part of a component
# is searched. Generous, because a leading matra can push the bar down.
DEVANAGARI_TOP_BAND = 0.40
# Below this a component is punctuation or a speck, never a word.
DEVANAGARI_MIN_WIDTH_PX = 8
# Grow the whitened area slightly past the component, to take the
# antialiased grey fringe that sits just outside the ink.
DEVANAGARI_PAD_PX = 2

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
    
def _remove_outer_card_lines(image: Any) -> Any:
    """Remove the four outer rules from a cropped card image. Interior strokes are preserved. Rules must be dark across most edge rows/columns."""
    import cv2
    import numpy as np

    if image is None or image.size == 0:
        return image
    if len(image.shape) == 2:
        gray = image
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    if height < 3 or width < 3:
        return image

    dark = gray < 200
    border_mask = np.zeros_like(gray, dtype=np.uint8)
    # The card frame does not sit on the crop edge. Measured on page 3 with
    # tests/test_scripts/measure_card_content.py it lies about 8px in from the
    # top and bottom and 20px from the left and right at 200 DPI, so a window
    # of 5 examined blank paper and could never fire. These margins reach the
    # frame at both 200 and 300 DPI; raise them if a template ever frames its
    # cards further in.
    edge_rows = min(CARD_FRAME_MARGIN_ROWS_PX, height)
    edge_cols = min(CARD_FRAME_MARGIN_COLS_PX, width)
    horizontal_fraction = 0.78
    vertical_fraction = 0.86

    for y in list(range(edge_rows)) + list(range(max(0, height - edge_rows), height)):
        if np.count_nonzero(dark[y, :]) / width >= horizontal_fraction:
            border_mask[max(0, y - 1):min(height, y + 2), :] = 255
    for x in list(range(edge_cols)) + list(range(max(0, width - edge_cols), width)):
        if np.count_nonzero(dark[:, x]) / height >= vertical_fraction:
            border_mask[:, max(0, x - 1):min(width, x + 2)] = 255

    cleaned = image.copy()
    cleaned[border_mask > 0] = 255
    # Keep the cleaned OCR input consistent with the existing production
    # Tesseract preprocessing: remove the printed photo placeholder and label.
    cleaned = _mask_photo_box(cleaned)
    return cleaned


def _crop_card_region(image: Any, region: tuple[float, float, float, float],
                      name: str) -> Any:
    """Cut a card-relative band from a cleaned card image. Ratios are fractions of card dimensions. Returns None if band falls off crop."""
    if image is None or image.size == 0:
        return None
    height, width = image.shape[:2]
    x0, y0, x1, y1 = region
    px0, px1 = int(width * x0), int(width * x1)
    py0, py1 = int(height * y0), int(height * y1)
    # Clamp, because a ratio of exactly 1.0 on a card whose width rounds down
    # would otherwise slice to an empty tail.
    px0, px1 = max(0, min(px0, width - 1)), max(0, min(px1, width))
    py0, py1 = max(0, min(py0, height - 1)), max(0, min(py1, height))
    if px1 - px0 < 2 or py1 - py0 < 2:
        log.debug(
            "%s collapsed on this card: %s -> x %d..%d y %d..%d of %dx%d",
            name, region, px0, px1, py0, py1, width, height)
        return None
    return image[py0:py1, px0:px1]


def _extract_text_with_tesseract(img_bytes: bytes) -> Optional[list[str]]:
    """Read a card image with Hindi Tesseract, returning lines. Image should be pre-cleaned; reads grayscale and Otsu variants, scores by Devanagari chars and labels."""
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None

        # Upscaling helps Tesseract's LSTM see Devanagari strokes; it works on
        # an estimated character height, and at card scale that estimate is
        # small enough to lose the matras below the headline.
        img_resized = cv2.resize(img, (0, 0), fx=2, fy=2,
                                 interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2)

        candidates: list[list[str]] = []
        for variant in (gray, thresh):
            text = pytesseract.image_to_string(
                Image.fromarray(variant),
                lang=TESSERACT_HINDI_LANG,
                config=TESSERACT_CONFIG,
            )
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if lines:
                candidates.append(lines)

        if not candidates:
            return None

        return max(candidates, key=_hindi_line_score)
    except Exception as exc:
        log.debug("Hindi Tesseract OCR failed: %s", exc)
        return None


# Hindi labels that a clean read of a voter card band contains. Each is worth
# more than a run of ordinary characters, because a band that has lost them
# has usually been over-thresholded into unreadable fused glyphs -- which
# still looks like Devanagari to a raw character count.
HINDI_FIELD_LABELS = (
    ("नाम", 10),                      # name
    ("पिता", 20), ("पति", 20),         # father, husband
    ("माता", 20), ("अन्य", 10),        # mother, other
    ("मकान", 10),                     # house
    ("आयु", 10),                      # age
    ("लिंग", 10),                     # gender
)


def _hindi_line_score(lines: list[str]) -> int:
    """Rank competing reads by Devanagari character count plus printed label weights. Higher is better; separates clean reads from noise."""
    text = " ".join(lines)
    score = len(re.findall(r"[ऀ-ॿ]", text))
    for label, weight in HINDI_FIELD_LABELS:
        if label in text:
            score += weight
    return score


def _ink_line_of_text(image: Any, min_height_frac: float = 0.35,
                      gap_frac: float = 0.7, rightmost: bool = True) -> Any:
    """Isolate printed characters in a header crop by removing box rules and keeping character-sized components. Returns rightmost word on white canvas."""
    gray = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    ink = (gray < 170).astype(np.uint8)
    h_len = max(15, int(width * 0.35))
    v_len = max(10, int(height * 0.5))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len)))
    rules = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    text = ink.copy()
    text[rules > 0] = 0

    count, labels, stats, _ = cv2.connectedComponentsWithStats(text, connectivity=8)  # type: ignore[call-overload]
    boxes = [tuple(stats[i][:4]) for i in range(1, count) if stats[i][4] >= 6]
    if not boxes:
        return None
    tallest = max(b[3] for b in boxes)
    boxes = [b for b in boxes if b[3] >= tallest * min_height_frac]
    if not boxes:
        return None
    boxes.sort()
    groups = [[boxes[0]]]
    for b in boxes[1:]:
        previous_right = max(x + w for x, _, w, _ in groups[-1])
        if b[0] - previous_right > tallest * gap_frac:
            groups.append([b])
        else:
            groups[-1].append(b)
    group = groups[-1] if rightmost else max(groups, key=len)
    x0 = min(x for x, _, _, _ in group); x1 = max(x + w for x, _, w, _ in group)
    y0 = min(y for _, y, _, _ in group); y1 = max(y + h for _, y, _, h in group)
    canvas = np.full((y1 - y0, x1 - x0), 255, np.uint8)
    keep = np.isin(labels[y0:y1, x0:x1],
                   [i for i in range(1, count)
                    if x0 <= stats[i][0] and stats[i][0] + stats[i][2] <= x1
                    and y0 <= stats[i][1] and stats[i][1] + stats[i][3] <= y1
                    and stats[i][3] >= tallest * min_height_frac])
    canvas[keep] = gray[y0:y1, x0:x1][keep]
    pad = max(10, canvas.shape[0] // 2)
    return cv2.copyMakeBorder(canvas, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def _read_line_variants(gray: Any, config: str,
                        targets: tuple[int, ...]) -> list[str]:
    """Read a line at several target heights (pixels), plain and Otsu-thresholded. ~30-45px glyph height works best for serial/EPIC reads."""
    out = []
    for target in targets:
        factor = target / max(1, gray.shape[0])
        big = cv2.resize(gray, (0, 0), fx=factor, fy=factor,
                         interpolation=cv2.INTER_CUBIC)
        for variant in (big, cv2.threshold(big, 0, 255,
                                           cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            text = pytesseract.image_to_string(
                Image.fromarray(variant), lang=TESSERACT_ENGLISH_LANG,
                config=config).strip()
            out.append(re.sub(r"\s+", "", text))
    return out


def _extract_serial_with_tesseract(img_bytes: bytes) -> Optional[list[str]]:
    """Extract serial number from top-left box. Returns list with digits or None. Removes box/markers, votes across scales/thresholds for reliability."""
    try:
        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        line = _ink_line_of_text(img)
        if line is None:
            return None
        reads = _read_line_variants(
            line, "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789",
            targets=(54, 72, 90))
        reads = [re.sub(r"\D", "", r) for r in reads if r]
        reads = [r for r in reads if r]
        if not reads:
            return None
        votes = Counter(reads)
        # Most common read; ties go to the longer one (a dropped digit is a
        # more common failure than an invented one on a clean crop).
        best = max(votes, key=lambda r: (votes[r], len(r)))
        return [best]
    except Exception as exc:
        log.debug("Serial OCR failed: %s", exc)
        return None


# An EPIC must be exactly three capital letters followed by seven digits,
# e.g. "GKM5933973". Any read that matches this shape is treated as valid
# and voted on; reads that don't match are only used as a fallback.
EPIC_SHAPE_RE = re.compile(r"^[A-Z]{3}\d{7}$")


def _extract_epic_with_tesseract(img_bytes: bytes) -> Optional[list[str]]:
    """Extract EPIC (3 letters + 7 digits) across scales/thresholds. Votes on correctly-shaped reads; returns most common cleaned read as fallback."""
    try:
        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        line = _ink_line_of_text(img, min_height_frac=0.5, gap_frac=1.5,
                                 rightmost=False)
        if line is None:
            return None
        reads = _read_line_variants(
            line,
            "--oem 3 --psm 7 -c tessedit_char_whitelist="
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            targets=(60, 80, 100, 120))
        reads = [_fix_epic_shape(r.upper()) for r in reads if r]
        if not reads:
            return None
        valid = [r for r in reads if EPIC_SHAPE_RE.match(r)]
        pool = valid or reads
        votes = Counter(pool)
        best = max(votes, key=lambda r: (votes[r], len(r)))
        return [best]
    except Exception as exc:
        log.debug("EPIC OCR failed: %s", exc)
        return None


def _extract_english_text_with_tesseract(img_bytes: bytes) -> Optional[list[str]]:
    """Read serial/EPIC box with English model (Latin chars only). Scales to readable height, applies _fix_epic_shape() to repair S-for-5 misreads."""
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None

        # A fixed 2x is right for the tall multi-line Hindi band and far too
        # small for a short crop like these header boxes, which are only a few
        # pixels tall. Scale each crop up to a readable height instead, never
        # down, so the tall band is unaffected.
        scale = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, img.shape[0])))
        img_resized = cv2.resize(img, (0, 0), fx=scale, fy=scale,
                                 interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)

        candidates: list[list[str]] = []
        for variant in (gray, cv2.threshold(gray, 0, 255,
                                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            text = pytesseract.image_to_string(
                Image.fromarray(variant),
                lang=TESSERACT_ENGLISH_LANG,
                config=TESSERACT_DIGIT_CONFIG,
            )
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if lines:
                candidates.append(lines)

        if not candidates:
            return None
        best = max(candidates, key=_english_line_score)
        return [_fix_epic_shape(line) for line in best]
    except Exception as exc:
        log.debug("English Tesseract OCR failed: %s", exc)
        return None


# An EPIC is three letters then digits, e.g. HR1234567. Anything in the digit
# run that comes back as a letter is a misread, not a value: on this template
# the LSTM renders 5 as S more often than anything else.
EPIC_PREFIX_LENGTH = 3
# 5 and S are the one pair worth repairing by name. Wider letter-to-digit
# substitution would need glyph evidence this function does not have, and
# guessing it would corrupt a read that was already right.
_EPIC_DIGIT_SUBSTITUTIONS = {"S": "5"}

# The printed label in front of the house value, "मकान संख्या". It is the same
# on every card, so it is matched and cut out before the value prefix is read
# -- otherwise the label is indistinguishable from a prefix. The trailing
# colon is part of the label, not of what follows it.
HOUSE_LABEL_RE = re.compile(r"मकान\s*संख्या\s*[:ः]?")


def _fix_epic_shape(text: str) -> str:
    """Force EPIC to 3 letters + digits. Keeps first 3 as letters, forces rest to digits (repairs S->5). Returns unchanged if too short."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", text)
    if len(cleaned) <= EPIC_PREFIX_LENGTH:
        return cleaned
    prefix = cleaned[:EPIC_PREFIX_LENGTH]
    rest = cleaned[EPIC_PREFIX_LENGTH:]
    rest = rest.translate(str.maketrans(_EPIC_DIGIT_SUBSTITUTIONS))
    # A letter with no mapping is dropped rather than guessed at, so the run
    # is digits only. Dropping loses a character; guessing loses correctness.
    rest = re.sub(r"[^0-9]", "", rest)
    return f"{prefix}{rest}"


def _house_digits_region(image: Any) -> Optional[tuple[int, int, int]]:
    """Find digit-only right-hand part of house crop by detecting Devanagari shirorekha boundary. Returns (x0, y0, x1) or None if no Devanagari found."""
    import cv2
    import numpy as np

    if image is None or image.size == 0:
        return None
    if len(image.shape) == 2:
        gray = image
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    if height < 3 or width < 3:
        return None

    scale = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, height)))
    big = cv2.resize(gray, (0, 0), fx=scale, fy=scale,
                     interpolation=cv2.INTER_CUBIC)
    dark = (big < DEVANAGARI_INK_LEVEL).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)  # type: ignore[call-overload]

    boundary = 0
    found = False
    for label in range(1, count):
        left, top, comp_w, comp_h, _ = stats[label]
        if comp_w < DEVANAGARI_MIN_WIDTH_PX * scale or comp_h < 3:
            continue
        band_end = top + max(3, int(comp_h * DEVANAGARI_TOP_BAND))
        component = (labels[top:band_end, left:left + comp_w] == label)
        if not component.any():
            continue
        if component.sum(axis=1).max() < comp_w * DEVANAGARI_BAR_RATIO:
            continue
        # Include the space after the word: the digits are set off from the
        # prefix, and starting the read on the last glyph of the prefix is
        # how a digit gets absorbed in the first place.
        edge = left + comp_w + int(DEVANAGARI_PAD_PX * scale)
        if edge > boundary:
            boundary, found = edge, True

    if not found:
        return None
    return (boundary // scale, 0, width)


def _strip_label_colon(image: Any) -> Any:
    """Whiten the label's leftover glyph and colon at left of house crop. Finds leftmost stacked dot pair (colon) and blanks up to it. Returns unchanged if no colon found."""
    import cv2
    import numpy as np

    if image is None or image.size == 0:
        return image
    gray = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    if height < 20 or width < 20:
        return image
    binary = (gray < 160).astype(np.uint8)
    count, _, stats, cents = cv2.connectedComponentsWithStats(binary, connectivity=8)  # type: ignore[call-overload]
    dots = []
    for i in range(1, count):
        left, top, w, h, area = stats[i]
        if h <= height * 0.22 and w <= height * 0.22 and area >= 4:
            dots.append((left, top, w, h, cents[i][0], cents[i][1]))
    dots.sort()
    for a in range(len(dots)):
        for b in range(a + 1, len(dots)):
            upper, lower = sorted((dots[a], dots[b]), key=lambda d: d[5])
            same_column = abs(upper[4] - lower[4]) <= height * 0.08
            spread = lower[5] - upper[5]
            if same_column and height * 0.2 <= spread <= height * 0.6 \
                    and upper[4] < width * 0.45:
                cut = int(max(upper[0] + upper[2], lower[0] + lower[2])
                          + height * 0.06)
                out = image.copy()
                out[:, :cut] = 255
                return out
    return image


def _tighten_house_crop(image: Any) -> Any:
    """Crop house band to its main ink line and pad with white. Removes blank margins and keeps tallest ink band for better single-line OCR."""
    import cv2
    import numpy as np

    if image is None or image.size == 0:
        return image
    gray = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    ink = gray < 160
    if not ink.any():
        return image
    rows = ink.sum(axis=1)
    peak = rows.max()
    # The digit line is the run of rows carrying real ink; slivers of the row
    # above carry a few pixels each.
    strong = np.where(rows >= max(2, peak * 0.15))[0]
    # Take the longest consecutive run of strong rows.
    splits = np.where(np.diff(strong) > 1)[0] + 1
    runs = np.split(strong, splits)
    band = max(runs, key=len)
    y0, y1 = int(band[0]), int(band[-1]) + 1
    line = ink[y0:y1]
    cols = np.where(line.any(axis=0))[0]
    if cols.size == 0:
        return image
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    pad = max(8, (y1 - y0) // 3)
    out = image[y0:y1, x0:x1]
    value = 255 if len(image.shape) == 2 else (255, 255, 255)
    return cv2.copyMakeBorder(out, pad, pad, pad, pad,
                              cv2.BORDER_CONSTANT, value=value)


def _extract_house_number_with_tesseract(img_bytes: bytes,
                                        raw: bool = False) -> Optional[str]:
    """Read house-number crop with English model (digits only). Uses Latin whitelist, returns longest contiguous digit run. raw=True returns raw text for calibration."""
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None

        # Scale to a readable height rather than by a fixed factor.
        scale = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, img.shape[0])))
        img_scaled = cv2.resize(img, (0, 0), fx=scale, fy=scale,
                                interpolation=cv2.INTER_CUBIC)

        # First pass: read the raw scaled image with English no-whitelist to
        # catch full fractions like '1/1044' before label-stripping destroys
        # the narrow leading '1'.
        gray_raw = cv2.cvtColor(img_scaled, cv2.COLOR_BGR2GRAY)
        for _cfg in ("--oem 3 --psm 7", "--oem 3 --psm 6"):
            txt_raw = pytesseract.image_to_string(
                Image.fromarray(gray_raw), lang=TESSERACT_ENGLISH_LANG,
                config=_cfg,
            ).strip()
            if raw:
                return txt_raw or None
            # Only extract fractions from the raw pass — single digits may
            # come from the label and are handled by the whitelist pass below.
            m_frac = re.search(r"(\d{1,5})\s*/\s*(\d{1,4})", txt_raw)
            if m_frac:
                return f"{m_frac.group(1)}/{m_frac.group(2)}"

        img_resized = _strip_label_colon(img_scaled)

        # Try to crop to digit-only region using Devanagari boundary detection
        digits_region = _house_digits_region(img_resized)
        if digits_region:
            x0, y0, x1 = digits_region
            img_resized = img_resized[y0:, x0:x1] if x1 > x0 else img_resized

        img_resized = _tighten_house_crop(img_resized)

        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)

        variants = (gray, cv2.threshold(gray, 0, 255,
                                       cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
        for variant in variants:
            text = pytesseract.image_to_string(
                Image.fromarray(variant),
                lang=TESSERACT_ENGLISH_LANG,
                config=TESSERACT_HOUSE_CONFIG,
            )
            if raw:
                return text.strip() or None
            runs = re.findall(r"\d+", text)
            if runs:
                return max(runs, key=len)

        # Fallback: digits_region / tighten_crop may have cut too aggressively
        # (e.g. '1/1044' where the narrow '1' sits right after the colon).
        # Re-read the scaled-but-unprocessed image with the full whitelist.
        scale2 = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, img.shape[0])))
        img_full = cv2.resize(img, (0, 0), fx=scale2, fy=scale2,
                              interpolation=cv2.INTER_CUBIC)
        gray_full = cv2.cvtColor(img_full, cv2.COLOR_BGR2GRAY)
        for variant in (gray_full,
                        cv2.threshold(gray_full, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            text = pytesseract.image_to_string(
                Image.fromarray(variant),
                lang=TESSERACT_ENGLISH_LANG,
                config=TESSERACT_HOUSE_CONFIG,
            )
            if raw:
                return text.strip() or None
            runs = re.findall(r"\d+", text)
            if runs:
                return max(runs, key=len)
        return None
    except Exception as exc:
        log.debug("English house-number OCR failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# House number: combine Hindi prefix + best digit string.
# ---------------------------------------------------------------------------
# Characters the Hindi model emits when the printed glyph is a Latin "1".
# Danda/double-danda/bar/bracket all look like a stem. "॥" is two stems.
_ONE_LOOKALIKES = {"॥": "1", "।": "1", "|": "1", "]": "1", "[": "1",
                   "I": "1", "l": "1", "!": "1"}
# Devanagari LETTERS and signs only: U+0900-U+0963. This deliberately stops
# before the danda (U+0964/5) and the Devanagari digits (U+0966-F), which the
# old range [ऀ-ॿ] included -- that is how a stray "१" became the "prefix".
_DEV_LETTER = "\u0900-\u0963"
_HOUSE_PREFIX_RUN_RE = re.compile(
    rf"[{_DEV_LETTER}][{_DEV_LETTER} .,]*")
_HOUSE_ANY_LABEL_RE = re.compile(r"(?:म|प्र)कान\s*संख्या\s*[:ः;*]?")
# Tesseract's filler words. It hallucinates a plausible Hindi syllable after a
# value it could not read — the house band for 'ई-1/75' came back as
# '... हु है' -- and a prefix is taken from the LAST Devanagari run on the
# line, so that filler was winning and displacing the real 'इ'.
_PREFIX_FILLER_RE = re.compile("|".join(re.escape(w) for w in
                                        ("हु", "है", "हुहु", "हे", "हो",
                                         "ही", "हु है", "हैं", "हुं", "ऐ",
                                         "ओ", "आ", "व")))


def _house_line(lines: Optional[list[str]]) -> str:
    """Return the house row line with label removed. Falls back to third line if label not found."""
    if not lines:
        return ""
    line = next((l for l in lines if _HOUSE_ANY_LABEL_RE.search(l)), None)
    if line is None:
        line = lines[2] if len(lines) >= 3 else (lines[0] if lines else "")
    return _HOUSE_ANY_LABEL_RE.sub(" ", line).strip()


def _clean_house_ocr_noise(value: str) -> str:
    """Clean common OCR noise from house numbers before returning."""
    if not value:
        return value
    # Strip known noise prefixes (with optional trailing characters)
    value = re.sub(r"^पाटकबबः\s*[व-ॿ]*\s*", "", value)
    # Normalize ऊ↔उ in "हाऊस"
    value = value.replace("हाऊस", "हाउस")
    # Normalize nukta variations in "नं"
    value = re.sub(r"न॑", "नं", value)
    value = re.sub(r"नें\.", "नं.", value)
    # Normalize इ↔1 in digit positions
    value = re.sub(r"नं इ", "नं 1", value)
    # Strip extra commas (both ASCII and Devanagari)
    value = re.sub(r"\s*[,،]\s*", " ", value)
    return value.strip()


def _house_prefix(line: str) -> str:
    """Extract Devanagari prefix from house line. Handles anusvara normalization and Latin prefixes like E-854."""
    runs = _HOUSE_PREFIX_RUN_RE.findall(line)
    if runs:
        p = runs[-1].strip().replace(",", ".")
        p = re.sub("\u0902+", "\u0902", p.replace("\u0951", "\u0902"))
        # Filler is not a prefix; keep looking for one further left.
        if not _PREFIX_FILLER_RE.fullmatch(p):
            return p
    m = re.search(r"([A-Za-z€£])\s*[-–]\s*\d", line)
    if m:
        return "E" if m.group(1) in "€£" else m.group(1).upper()
    return ""


def _house_tail_text(line: str) -> tuple[str, bool]:
    """Extract text after Devanagari prefix, with trust flag for lookalikes. Cuts at last colon if no prefix found."""
    last = None
    for m in re.finditer(rf"[{_DEV_LETTER}]", line):
        last = m
    if last:
        return line[last.end():], True
    colon = max(line.rfind(c) for c in ":ः;")
    if colon >= 0:
        return line[colon + 1:], True
    return line, False


def _house_tail_digits(line: str) -> list[tuple[str, str]]:
    """Extract digits from tail, tagging as 'D' (real digit) or 'L' (lookalike like ॥)."""
    tail, trust_lookalikes = _house_tail_text(line)
    out: list[tuple[str, str]] = []
    for ch in tail:
        if ch.isdigit():                      # ASCII and Devanagari digits
            out.append((str(int(ch)), "D"))
        elif ch in _ONE_LOOKALIKES and trust_lookalikes:
            out.extend((c, "L") for c in _ONE_LOOKALIKES[ch])
    return out


def _house_slash_number(lines: list[str]) -> Optional[str]:
    """Extract prefix-less slash number like '132/10' from Hindi reads. Requires 2+ agreeing reads (English whitelist has no '/')."""
    found: list[str] = []
    for line in lines:
        if re.search(rf"[{_DEV_LETTER}]", line):
            continue                      # "इ /8 486": slash belongs to prefix
        tail, _ = _house_tail_text(line)
        m = re.search(r"(?<!\d)(\d{1,5})\s*/\s*(\d{1,4})(?!\d)", tail)
        if m:
            found.append(f"{m.group(1)}/{m.group(2)}")
        # Also catch a leading-1-dropped slash: '/044' when '1/044' was printed.
        # The narrow '1' before a slash is a known OCR drop.
        m2 = re.search(r"(?<!\d)/\s*(\d{2,4})(?!\d)", tail)
        if m2:
            found.append(f"1/{m2.group(1)}")
    return next((f for f in found if found.count(f) >= 2), None)


def _digits_from_english_raw(raw: Optional[str], fallback: Optional[str]
                             ) -> Optional[str]:
    """Extract number from English raw text. Takes digits after last hyphen, or fraction like 132/10. Returns fallback if no match."""
    if raw:
        # A fraction-style number such as "132/10" (no prefix on the row).
        # Kept whole, slash included; the caller decides whether a prefix
        # makes that slash suspect. The fraction must END the tail: the crop
        # overruns into the serial box often enough to append its digits
        # ("1/750 0" for serial 528), and those were being glued onto the
        # denominator as "1/7500".
        frac = re.search(r"(?<!\d)(\d{1,5})\s*/\s*(\d{1,4})\s*$",
                         raw.rsplit("-", 1)[-1])
        if frac:
            return f"{frac.group(1)}/{frac.group(2)}"
    if raw and "-" in raw:
        runs = re.findall(r"\d+", raw.rsplit("-", 1)[1])
        if runs:
            return "".join(runs)
    return fallback


# How many digits the Devanagari model may have dropped from one run. Every
# case seen so far loses exactly one — '146'->'46' (leading), '701'->'71'
# (middle), '190'->'90' — so a bigger gap is a different number, not a
# mangled read of the same one.
_MAX_DIGITS_DROPPED = 1


def _same_number_two_reads(paddle_run: str, text_run: str) -> bool:
    """True when two digit runs are the same number read with errors.

    The Hindi model drops characters outright and can drop one from the
    middle, so containment is the wrong test: '71' is not a substring of
    '701' even though '701' is plainly what was printed. What must hold is
    that the shorter run appears inside the longer one *in order*, with at
    most one character skipped.

    Order and the gap cap are what keep a coincidence out. '75' is nowhere
    inside '46', and '46' is not reachable from '1469' without skipping
    more than one digit, so neither is mistaken for a mangled read. Runs of
    equal length instead get one differing position, a digit misread as its
    neighbour, and no differences at all — identical is aligned, because one
    run can be a recovery while the next was read correctly, and a strict
    test there would veto the recovery.
    """
    if not paddle_run or not text_run:
        return False
    short, long_ = sorted((paddle_run, text_run), key=len)
    gap = len(long_) - len(short)
    if gap == 0:
        return sum(1 for a, b in zip(paddle_run, text_run) if a != b) <= 1
    if gap > _MAX_DIGITS_DROPPED:
        return False
    matched = 0
    for ch in long_:
        if matched < len(short) and ch == short[matched]:
            matched += 1
    return matched == len(short)


# A digit misread as a Devanagari mark: '।' and '॥' are what a trailing 1
# turns into, and a bare '!' is a 1 the recognizer gave up on. Only ever
# treated as digits inside the free-text merge, where Paddle's independent
# read has to confirm the result — the global lookalike fix is left alone
# because it has no such check and fires on every page.
# '॥' is two characters wide but stands for one lost digit, so the whole
# run collapses to a single '1' rather than one per character.
# ']' and '[' are the same case: '_ONE_LOOKALIKES' above already lists them,
# but that map is only reachable on the non-free-text path, so a free-text
# house like 'इ-]/75' kept the bracket. Scoped here for the same reason.
_DIGIT_LOOKALIKE = "।॥|!]["
_LOOKALIKE_RUN = re.compile(f"[{re.escape(_DIGIT_LOOKALIKE)}]+")


def _merge_text_with_digits(
    text_value: str,
    paddle_house: str,
    paddle_texts: list[str],
    corroborating_runs: Optional[list[str]] = None,
) -> str:
    """Restore numerals that the Devanagari model dropped from a free-text house.

    Tesseract's Hindi model systematically loses Latin digits and letters:
    'एचएनजीओ 146' reads as 'एचएनओ 46', '190' as '90', 'ई-1/75' as 'इ-/75'.
    The pixels are fine — the loss happens inside the recognizer, which is why
    re-OCRing at other scales never recovers it. PaddleOCR reads every one of
    those characters, but as a digits-only run with the Devanagari stripped,
    so it cannot be used to replace the whole value.

    The fix is per character class: keep Tesseract's Devanagari skeleton and
    splice Paddle's numerals back in. Returns "" when the two cannot be
    aligned, so the caller can fall back to its previous behaviour.

    `corroborating_runs` are digit runs from Tesseract's own wider house-band
    read, not Paddle's. They matter for a digit that left no run of its own to
    align against: in 'इ-/75' the missing '1' has nothing to the left of it, so
    the positional splice can never recover it — yet the same card's wider read
    shows '] /75', the bracket being how this model renders a printed '1'. When
    a corroborating run both matches the Tesseract run and appears in Paddle's
    read, the leading digit is put back from that evidence.
    """
    if not text_value:
        return ""
    if not re.search(r"[ऀ-ॿ]", text_value):
        return ""
    # A digit read as a danda lookalike is still a digit run for alignment
    # purposes: 'ख नं-70॥' holds the same two numbers as 'ख नं-701'. Promote
    # those to '1' so the positional splice can align them. A promotion with
    # no confirmed merge is discarded, so a stray '।' in the label is safe.
    had_lookalike = bool(_LOOKALIKE_RUN.search(text_value))
    if had_lookalike:
        text_value = _LOOKALIKE_RUN.sub("1", text_value)

    def _no_merge() -> str:
        """Reject the merge, keeping the original text unless a lookalike was
        promoted and Paddle independently read a matching number there."""
        if not had_lookalike:
            return ""
        return text_value

    text_runs = re.findall(r"\d+", text_value)
    # Paddle's full text lines, not just the winning run — a digit that lost
    # its neighbours in the Hindi read is usually still sitting in one of
    # these next to its neighbours.
    paddle_runs: list[str] = []
    for pt in paddle_texts:
        paddle_runs.extend(re.findall(r"\d+", pt))
    if not paddle_runs:
        paddle_runs = re.findall(r"\d+", paddle_house or "")
    if not paddle_runs:
        return _no_merge()

    # Replace each Tesseract digit run with the Paddle run in the same
    # position when Paddle read the same count of numbers. Matching on count
    # rather than on value is the point: Tesseract's '46' and Paddle's '146'
    # are the *same* number with a lost leading digit, so an equality test
    # would reject exactly the case this exists to fix.
    #
    # Each pair must still be recognizably the same number — one has to
    # contain the other in order, or differ in a single position. Without
    # that check a coincidental count match rewrites one house number into
    # another ('इ-/75' + '46' -> 'इ-/46'), which is worse than leaving it
    # alone.
    #
    # Paddle's line often holds more runs than the Hindi read — it sees the
    # label glyphs as stray digits too ('41:3-1/75' for 'इ-1/75', where '41'
    # and '3' are misread Devanagari). So rather than requiring equal counts,
    # take the window of Paddle runs that lines up with the Hindi read's runs.
    #
    # The window has to be at least as long as the Tesseract run it replaces.
    # Without that, a one-digit Paddle read of a two-digit value ('5' for
    # '75', from the value crop of 'इ-1/75') counts as aligned and rewrites a
    # correct number into a wrong one. A recovery never shortens a run — it
    # only ever puts back digits the model dropped — so the shorter reading is
    # the wrong one.
    span = len(text_runs)
    chosen: list[str] = []
    merged: str = ""
    if span and len(paddle_runs) >= span:
        for start in range(len(paddle_runs) - span + 1):
            window = paddle_runs[start:start + span]
            pairs = list(zip(window, text_runs))
            aligned = (all(len(p) >= len(t) for p, t in pairs)
                       and all(_same_number_two_reads(p, t) for p, t in pairs))
            log.debug("free-text house merge window %d: pairs=%s aligned=%s",
                      start, pairs, aligned)
            if not aligned:
                continue
            out, idx = [], 0
            for chunk in re.split(r"(\d+)", text_value):
                if chunk.isdigit():
                    out.append(window[idx])
                    idx += 1
                else:
                    out.append(chunk)
            spliced = "".join(out)
            # A window that changes nothing is not a merge — 'इ-/75' against
            # Paddle's '75' is the same number, and stopping there would hide
            # the leading digit the next branch can recover. Keep looking.
            if spliced == text_value:
                continue
            # A merge that loses characters is a misalignment.
            if len(spliced) < len(text_value):
                continue
            log.debug("free-text house merge: %r + %s -> %r",
                      text_value, window, spliced)
            merged = spliced
            break
    if merged:
        return merged
    if span and len(paddle_runs) >= span:
        log.debug("free-text house merge declined: %d text runs vs %s paddle=%s",
                  span, len(paddle_runs), paddle_runs)

    # A leading digit the model dropped leaves no run of its own to align
    # against: 'इ-/75' has one run ('75') and Paddle's '1/75' has a digit the
    # Hindi read simply does not contain, so no window can line up. The
    # recovery comes from agreement between the two engines on what surrounds
    # it. The corroborating band spells the printed number out as its parts —
    # '] /75' promotes to runs '1' and '75', with the slash still between
    # them — so the test is that those parts sit next to each other in the
    # order Paddle read them.
    #
    # Every part has to be accounted for. Checking the recovered digits alone
    # is not enough: '] /75' also contains '75' on its own, and that matches
    # the digits the Hindi read already has, so it would restore a leading
    # digit onto every value whose first run Paddle read. Requiring each part
    # to be either already present or a single dropped leading digit is what
    # keeps it to the case that is actually missing one.
    if not chosen and corroborating_runs:
        log.debug("restore attempt: text=%r paddle_house=%r paddle_runs=%s "
                  "text_runs=%s corroborating=%s",
                  text_value, paddle_house, paddle_runs, text_runs,
                  corroborating_runs)
        for mnum in re.findall(r"\d+\s*/\s*\d+", paddle_house or ""):
            parts = re.findall(r"\d+", mnum)
            if not parts or len(parts) > _MAX_DIGITS_DROPPED + 1:
                continue
            # Every part is either a run the Hindi read already has, or one
            # extra leading digit this value is missing.
            extra = [p for p in parts if p not in text_runs]
            if len(extra) > _MAX_DIGITS_DROPPED:
                continue
            if not extra:
                continue
            if not all(p in corroborating_runs for p in parts):
                continue
            # The parts must appear in the corroborating read in order, which
            # is what a slash between them looks like.
            seq = [r for r in corroborating_runs if r in parts]
            if seq != parts:
                continue
            cand = extra[0]
            # Insert the digit where Paddle put it, which is before the slash
            # in 'इ-1/75' — the extra part leads Paddle's slash number. Slicing
            # at at+1 kept the slash and put the digit after it, yielding
            # 'इ-/175'.
            at = text_value.find("/")
            if at < 0 or not re.search(r"\d", text_value[at + 1:]):
                continue
            merged = f"{text_value[:at]}{cand}{text_value[at:]}"
            if len(merged) > len(text_value):
                log.debug("free-text house merge: leading %r restored from "
                          "corroborating parts %s", cand, parts)
                return merged
    return _no_merge()


def _combine_house_read(hindi_fields_lines: Optional[list[str]],
                        english_digits: Optional[str],
                        house_region_lines: Optional[list[str]] = None,
                        house_value_lines: Optional[list[str]] = None,
                        english_raw: Optional[str] = None) -> Optional[str]:
    """Combine Hindi prefix (majority vote) with English digits, recovering leading digits via lookalikes or agreement across reads."""
    base = _digits_from_english_raw(english_raw, english_digits)
    lines = [_house_line(hindi_fields_lines),
             _house_line(house_region_lines),
             _house_line(house_value_lines)]
    lines = [l for l in lines if l]

    # ---- prefix ----
    def key(p: str) -> str:
        return re.sub(r"[ .,]", "", p)
    cands = [p for p in (_house_prefix(l) for l in lines) if p]
    prefix = ""
    if cands:
        counts: dict[str, int] = {}
        for p in cands:
            counts[key(p)] = counts.get(key(p), 0) + 1
        best = max(counts.values())
        winner = next(p for p in cands if counts[key(p)] == best)
        prefix = winner

    # ---- digits ----
    if base and "/" in base:
        # Only trust a slash on a row with no prefix ("132/10"). With a prefix
        # the slash belongs to it ("इ /8 486") and the number is digits only.
        if not prefix:
            return base
        base = re.sub(r"\D", "", base)
    if not base:
        for l in lines:
            d = "".join(c for c, _ in _house_tail_digits(l))
            if d:
                base = d
                break
    if not base:
        return None

    # The house number is the LAST thing on the row; the crop can overrun
    # into the serial box and append its digits. '1:3-1/750 0' is serial 528
    # bleeding into 'इ-1/75', and it produced '17500' -- a number no read
    # supports. Take the longest trailing group of digit runs, since the
    # extra ones always land at the front of the row, not after the value.
    runs = re.findall(r"\d+", base)
    if len(runs) > 1:
        for keep in range(len(runs), 1, -1):
            tail = "".join(runs[-keep:])
            if any(line.rstrip().endswith(tail) for line in lines):
                base = tail
                break

    slash = _house_slash_number(lines)
    if slash:
        a, b = slash.split("/")
        if base in (a, b) or (a + b).endswith(base) or a.endswith(base):
            return f"{prefix} {slash}".strip()
        # a+b == base means English read the full concatenated digits (e.g.
        # '1044') while Hindi slash has '1/044' — reconstruct as a/base[len(a):]
        # which gives the correct '1/1044'.
        if a + b == base and len(base) > len(b):
            return f"{prefix} {a}/{base[len(a):]}".strip()

    extras: list[str] = []
    lookalike_lead = ""
    for l in lines:
        tail = _house_tail_digits(l)
        s = "".join(c for c, _ in tail)
        if len(s) > len(base) and s.endswith(base):
            cut = tail[:len(s) - len(base)]
            extras.append("".join(c for c, _ in cut))
            run = ""
            for c, tag in reversed(cut):
                if tag != "L":
                    break
                run = c + run
            if len(run) > len(lookalike_lead):
                lookalike_lead = run
    agreed = next((e for e in extras if extras.count(e) >= 2), "")
    digits = (agreed or lookalike_lead) + base
    result = f"{prefix} {digits}".strip()
    return _clean_house_ocr_noise(result)


def _english_line_score(lines: list[str]) -> int:
    """Rank competing reads of a header box. Higher is better.

    Alphanumeric characters carry the weight: a box that read as empty or
    as a pile of punctuation has lost its value. Length breaks the tie
    between two plausible reads, since the printed boxes hold a fixed-width
    serial and EPIC and the longer run is nearly always the intact one.
    """
    text = " ".join(lines)
    score = len(re.findall(r"[A-Za-z0-9]", text))
    return score * 10 + len(text)



def _detect_deleted_watermark(card_img: Any, serial_png: Optional[bytes] = None) -> bool:
    """Detect DELETED watermark via Q prefix in the serial box.

    Fast path: Tesseract checks for letters in the serial box first.
    Only falls through to PaddleOCR when letters are present (Q marker).
    """
    try:
        if serial_png is None:
            return False
        nparr = np.frombuffer(serial_png, np.uint8)
        serial_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if serial_img is None:
            return False

        # Fast Tesseract pre-check — normal serials are digits only.
        # Run PaddleOCR when: letters present (possible Q) OR Tesseract empty
        # (Tesseract misses Q on some cards like card 1 of page 3).
        gray = cv2.cvtColor(serial_img, cv2.COLOR_BGR2GRAY)
        quick_txt = pytesseract.image_to_string(
            Image.fromarray(gray), lang="eng", config="--oem 3 --psm 7"
        ).strip().upper()
        digits_only = bool(re.fullmatch(r"[\d\s]*", quick_txt))
        if digits_only and quick_txt:
            return False  # confident it's just digits — not deleted

        # Letters found — confirm with PaddleOCR (much more reliable on Q)
        if PaddleOCR is not None:
            try:
                result = _get_paddle().ocr(serial_img, cls=False)
                texts = [line[1][0].upper() for line in (result[0] or []) if line[1][0]]
                if any("Q" in t for t in texts):
                    return True
            except Exception:
                pass
        return False
    except Exception as exc:
        log.debug("Deleted-watermark detection failed: %s", exc)
        return False


def _empty_record() -> dict[str, Any]:
    return {
        "sno": "", "id_card_no": "", "gender": "", "age": "",
        "house_no": "", "voter_first_name": "",
        "voter_middle_name": "", "voter_sur_name": "",
        "relation_name": "", "voter_husband_name": "",
        "voter_father_name": "", "voter_mother_name": "",
        "voter_other_name": "",
    }


def _extract_tesseract_epic(img_bytes: bytes) -> str:
    """Read EPIC from card image bytes for voter grid probe. Returns empty string on failure."""
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return ""
        cl = _remove_outer_card_lines(img)
        epic_crop = _crop_card_region(cl, TESSERACT_CARD_CONTENT_REGIONS[EPIC_REGION_INDEX], "EPIC")
        if epic_crop is None or epic_crop.size == 0:
            return ""
        ok, enc = cv2.imencode(".png", epic_crop)
        if not ok:
            return ""
        reads = _extract_epic_with_tesseract(enc.tobytes())
        return reads[0] if reads and reads[0] else ""
    except Exception as exc:
        log.debug("Tesseract EPIC probe failed: %s", exc)
        return ""



def parse_voter_box_from_ocr_lines(
    lines: list[str],
    relation_hint: str = "",
) -> dict[str, Any]:
    """Parse clean Hindi OCR output into voter record.

    Expects lines like:
      'नाम : नरेन्द्र सिंह'
      'पिता का नाम: ओमपाल सिंह'
      'मकान संख्या : ई-60'
      'आयु : 58 लिंग : पुरुष'
    """
    if not lines:
        return {"empty": True}

    _DIGITS_MAP = str.maketrans("०१२३४५६७८९", "0123456789")

    def clean_value(value: str) -> str:
        text = value.translate(_DIGITS_MAP)
        text = re.sub(r"[\u200b-\u200f\u00ad]", "", text)
        text = re.sub(r"\u094d{2,}", "\u094d", text)
        # \u091a\u0928\u094d\u0926\u094d\u0930 vs \u091a\u0902\u0926\u094d\u0930: the roll writes both (\u091a\u0928\u094d\u0926\u094d\u0930 \u0936\u0930\u094d\u092e\u093e, \u091a\u0902\u0926 \u092a\u093e\u0932), so
        # normalize whichever form Tesseract produced to \u091a\u0928\u094d\u0926\u094d\u0930. \u091a\u0902\u0926\u093e and
        # \u091a\u0928\u094d\u0926\u094d\u0930\u093e are a different name and must not be touched, so the
        # \u094d\u0930 conjunct is required.
        text = re.sub(r"\u091a\u0902\u0926\u094d\u0930(?!\u093e)", "\u091a\u0928\u094d\u0926\u094d\u0930", text)
        # Strip leading OCR punctuation noise (quotes, pipes, commas, visarga)
        text = re.sub(r"^['\",;|\u0964\u0965\u0903\s]+", "", text)
        return " ".join(text.split())

    def after_colon(line: str) -> str:
        # Include visarga ः (U+0903), ! and ; as colon variants — Tesseract
        # sometimes reads the printed colon as these characters.
        parts = re.split(r"[:：ः;!]", line, maxsplit=1)
        return clean_value(parts[1].strip()) if len(parts) > 1 else ""

    record: dict[str, Any] = {
        "sno": "", "id_card_no": "", "gender": "", "age": "",
        "house_no": "", "voter_first_name": "",
        "voter_middle_name": "", "voter_sur_name": "",
        "relation_name": "", "voter_husband_name": "",
        "voter_father_name": "", "voter_mother_name": "",
        "voter_other_name": "",
    }

    # The `नाम` in every relation label is the most misread glyph in the set —
    # Tesseract returns नाग (म's vertical + the ā bar read as ग) often enough
    # that an unmatched label silently drops the whole relation. Shared here
    # so all four patterns and the voter-name test stay in step.
    _नाम_LBL = r"(?:नाम|नाग|nama)"

    # Relation rows: (label pattern, canonical relation, record field)
    _RELATIONS = (
        (rf"(?:पिता|पेता|पित|प्रिता|Old)\s*(?:का)?\s*{_नाम_LBL}",  "पिता",  "voter_father_name"),
        # पति variants: प्रति/प्रत्ति (Tesseract misread), पत्ति (double त),
        # पत, and क्वा (conjunct misread that eats the whole label)
        (rf"(?:पति|पत्ति|प्रति|प्रत्ति|पत|क्वा)\s*(?:का)?\s*{_नाम_LBL}",  "पति",   "voter_husband_name"),
        (rf"(?:माता|मात|मोता|m[aā]t[aā])\s*(?:का)?\s*{_नाम_LBL}", "माता", "voter_mother_name"),
        # अन्य is the one label printed on its own — 'अन्य: सुनीता', with no
        # 'का नाम' — so the नाम label is optional here and nowhere else.
        (rf"(?:अन्य|अनय|anya|anye)\s*(?:का)?\s*(?:{_नाम_LBL})?\s*[:：;ः!]",
         "अन्य", "voter_other_name"),
    )

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Voter name — must NOT match a relation label on the same line
        if re.search(rf"{_नाम_LBL}\s*[:：;ः!]", line) and not any(
            re.search(pat, line, re.IGNORECASE) for pat, *_ in _RELATIONS
        ):
            name = after_colon(line)
            parts = name.split()
            if parts:
                record["voter_first_name"] = parts[0]
                if len(parts) == 2:
                    record["voter_sur_name"] = parts[1]
                elif len(parts) > 2:
                    record["voter_middle_name"] = " ".join(parts[1:-1])
                    record["voter_sur_name"] = parts[-1]
            continue

        # Relation rows — one loop replaces four nearly-identical blocks
        matched_relation = False
        for pat, canonical, field in _RELATIONS:
            if re.search(pat, line, re.IGNORECASE):
                name = after_colon(line)
                if name:
                    record["relation_name"] = canonical
                    record[field] = name
                matched_relation = True
                break
        if matched_relation:
            continue

        # House number
        if re.search(r"मकान\s*संख्या", line):
            record["house_no"] = after_colon(line)
            continue

        # Age and gender can appear on the same line: आयु : 58 लिंग : पुरुष
        if "आयु" in line:
            # Match ASCII or Devanagari digits (e.g. ३6, ३६)
            m = re.search(r"आयु\s*[:：]?\s*([\d०-९]{1,3})", line)
            if m:
                age_val = m.group(1).translate(str.maketrans("०१२३४५६७८९", "0123456789"))
                # Voters must be >= 18; single/double digit values below 18
                # are almost always Tesseract dropping a leading '1'.
                if int(age_val) >= 18:
                    record["age"] = age_val
        if "लिंग" in line:
            if "पुरुष" in line:
                record["gender"] = "पुरुष"
            # महिला and its OCR variants: Tesseract drops, doubles or
            # relocates the i-matra and inserts stray viramas, producing
            # महिल्ला / मछिला / महिला. Match the म...ला skeleton while
            # allowing any Devanagari marks in between — note \w does NOT
            # cover combining marks, so the class is spelled out.
            elif re.search(r"म[ऀ-ॿ]*ल[ऀ-ॿ]*ा|mahila", line):
                record["gender"] = "महिला"

    if any(record.get(k) for k in ("voter_first_name", "voter_father_name", "age", "gender")):
        return record
    return {"empty": True}

def _extract_card(
    page: fitz.Page,
    card_rect: fitz.Rect,
    page_number: Optional[int] = None,
    card_index: Optional[int] = None,
    rendered_images: Optional[bytes] = None,
) -> Optional[dict[str, Any]]:
    """Extract voter data from a single card: serial, EPIC, Hindi fields.
    Returns None if no valid data extracted.

    Set the environment variable OCR_TRACE=1 to print every pipeline step
    to stdout.  Leave it unset (or set to 0) for silent operation.
    """
    _TRACE = os.getenv("OCR_TRACE", "0") == "1"
    _pfx = f"[TRACE p={page_number} c={card_index}]"

    def _t(step: str, value: Any) -> None:
        """Print one pipeline step and its output when tracing is on."""
        if _TRACE:
            print(f"{_pfx} {step:45s} => {value!r}", flush=True)

    def _crop_png(img: Any, region: tuple, label: str) -> Optional[bytes]:
        crop = _crop_card_region(img, region, label)
        if crop is None:
            _t(f"_crop_card_region({label})", None)
            return None
        ok, enc = cv2.imencode(".png", crop)
        result = enc.tobytes() if ok else None
        _t(f"_crop_card_region({label})", f"<{len(result)} bytes>" if result else None)
        return result

    try:
        # ── 1. Render card from PDF ──────────────────────────────────────
        # Use the pre-rendered crop when available (sliced from a single
        # full-page render), otherwise fall back to a per-card pixmap call.
        if rendered_images is not None:
            hindi_bytes = rendered_images
        else:
            hindi_bytes = page.get_pixmap(dpi=200, clip=card_rect, alpha=False).tobytes("png") # type: ignore
        _t("page.get_pixmap → PNG", f"<{len(hindi_bytes)} bytes>")

        card_img = cv2.imdecode(np.frombuffer(hindi_bytes, np.uint8), cv2.IMREAD_COLOR)
        if card_img is None or card_img.size == 0:
            _t("cv2.imdecode", None)
            return None
        _t("cv2.imdecode", f"<image {card_img.shape}>")

        # ── 2. Clean card image ──────────────────────────────────────────
        cl = _remove_outer_card_lines(card_img)
        _t("_remove_outer_card_lines", f"<image {cl.shape}>")

        # ── 3. Serial number ─────────────────────────────────────────────
        sno = ""
        serial_png = _crop_png(cl, TESSERACT_CARD_CONTENT_REGIONS[SERIAL_REGION_INDEX], "SERIAL")
        if serial_png:
            s_reads = _extract_serial_with_tesseract(serial_png)
            _t("_extract_serial_with_tesseract", s_reads)
            if not (s_reads and s_reads[0]):
                s_reads = _extract_english_text_with_tesseract(serial_png)
                _t("_extract_english_text_with_tesseract (serial fallback)", s_reads)
            if s_reads and s_reads[0]:
                sno = s_reads[0]
        _t("serial → sno", sno)

        # ── 4. EPIC / Voter ID ───────────────────────────────────────────
        id_card_no = ""
        epic_png = _crop_png(cl, TESSERACT_CARD_CONTENT_REGIONS[EPIC_REGION_INDEX], "EPIC")
        if epic_png:
            e_reads = _extract_epic_with_tesseract(epic_png)
            _t("_extract_epic_with_tesseract", e_reads)
            if not (e_reads and e_reads[0]):
                e_reads = _extract_english_text_with_tesseract(epic_png)
                _t("_extract_english_text_with_tesseract (EPIC fallback)", e_reads)
            if e_reads and e_reads[0]:
                id_card_no = e_reads[0]
        _t("EPIC → id_card_no", id_card_no)

        # ── 5. Hindi fields ──────────────────────────────────────────────
        hindi_fields_lines = None
        hindi_png = _crop_png(cl, TESSERACT_CARD_CONTENT_REGIONS[2], "HINDI_FIELDS")
        if hindi_png:
            hindi_fields_lines = _extract_text_with_tesseract(hindi_png)
            _t("_extract_text_with_tesseract (hindi crop)", hindi_fields_lines)
        if not hindi_fields_lines:
            ok, enc_cl = cv2.imencode(".png", cl)
            if ok:
                hindi_fields_lines = _extract_text_with_tesseract(enc_cl.tobytes())
                _t("_extract_text_with_tesseract (full card fallback)", hindi_fields_lines)

        # ── 6. Parse Hindi lines into record ─────────────────────────────
        record = parse_voter_box_from_ocr_lines(hindi_fields_lines or [])
        _t("parse_voter_box_from_ocr_lines", record)
        if record.get("empty"):
            record = _empty_record()
            _t("_empty_record (parse returned empty)", record)

        if sno:
            record["sno"] = sno
        if id_card_no:
            record["id_card_no"] = id_card_no

        # ── 7. House number ──────────────────────────────────────────────
        try:
            h_lines = None
            house_png = _crop_png(cl, HOUSE_REGION, "HOUSE")
            if house_png:
                h_lines = _extract_text_with_tesseract(house_png)
                _t("_extract_text_with_tesseract (HOUSE region)", h_lines)

            # Some rolls print the house as free text rather than a number
            # ('प्लॉट नं 279 ख नं 79', 'एचएनजीओ 146'). The digit-only reader
            # and the PaddleOCR override both mangle those — they strip
            # non-numerics out of the middle and drop narrow leading 1s —
            # so a Devanagari value is taken from the Devanagari read alone.
            parser_house_raw = record.get("house_no", "")
            house_is_text = bool(re.search(r"[ऀ-ॿ]", parser_house_raw))
            _t("house is free text (Devanagari)", house_is_text)

            hv_lines = None
            e_digit = None
            e_raw = None
            paddle_house = None  # PaddleOCR house value (trusted primary)
            hv_png = _crop_png(cl, HOUSE_VALUE_REGION, "HOUSE_VAL")
            if hv_png:
                hv_lines = _extract_text_with_tesseract(hv_png)
                _t("_extract_text_with_tesseract (HOUSE_VAL region)", hv_lines)
                e_digit = _extract_house_number_with_tesseract(hv_png)
                _t("_extract_house_number_with_tesseract (digits)", e_digit)
                e_raw = _extract_house_number_with_tesseract(hv_png, raw=True)
                _t("_extract_house_number_with_tesseract (raw)", e_raw)
                # PaddleOCR for house deferred to main thread (not thread-safe)
            # If HV returned nothing, try PaddleOCR on the wider HOUSE crop,
            # then fall back to Tesseract — catches single-digit values like
            # '1' that Tesseract LSTM misses on narrow glyphs.
            if not e_digit and house_png and not house_is_text:
                e_digit = _extract_house_number_with_tesseract(house_png)
                _t("_extract_house_number_with_tesseract (HOUSE fallback)", e_digit)

            combined = _combine_house_read(hindi_fields_lines, e_digit, h_lines, hv_lines, e_raw)
            _t("_combine_house_read", combined)

            parser_house = record.get("house_no", "")
            if house_is_text:
                # Free-text house: keep the longest Devanagari read. `combined`
                # is only a fragment of it (a whole house value can be longer
                # than the region the digit reader sees), so the fuller
                # parser value wins unless combined is genuinely longer.
                #
                # "Genuinely longer" is measured on the Devanagari content,
                # not the raw string: a digit reader that overruns the serial
                # box manufactures digits no read supports ('इ 17500' from
                # 'इ-1/75'), and those are exactly what made `combined` the
                # longer candidate. A candidate that carries the parser value's
                # letters is preferred; only a real letter-for-letter
                # replacement outranks it.
                def _devanagari_len(v: str) -> int:
                    return len(re.findall(rf"[{_DEV_LETTER}]", v or ""))

                if combined and _devanagari_len(combined) > _devanagari_len(parser_house):
                    record["house_no"] = combined
                elif parser_house:
                    record["house_no"] = parser_house
                elif combined:
                    record["house_no"] = combined
                _t("house_no (free text)", record.get("house_no", ""))
            elif combined:
                # Strip leading Devanagari label that leaked in
                cleaned = re.sub(r"^[^A-Za-z0-9]+", "", combined).strip()
                has_devanagari = bool(re.search(r"[ऀ-ॿ]", parser_house))
                # paddle_house or e_digit (from raw pass) is primary for pure digits
                best_numeric = paddle_house or (e_digit if e_digit and not has_devanagari else None)
                if best_numeric and not has_devanagari:
                    if "/" in cleaned and not cleaned.startswith("/") and len(cleaned) > len(best_numeric):
                        best = cleaned
                    else:
                        best = best_numeric
                elif ("/" in cleaned and not cleaned.startswith("/")
                        and (not parser_house or parser_house.startswith("/"))):
                    best = cleaned
                elif has_devanagari:
                    best = parser_house
                elif parser_house:
                    best = parser_house
                else:
                    best = cleaned if cleaned else combined
                best = re.sub(r"(?<!\d)[|\[\]।॥](?=\d|/|$)|(?<=\d)[|\[\]।॥](?=\d|/|$)|(?<=/)[\[\]।॥]", "1", best)
                record["house_no"] = best
            elif paddle_house and not re.search(r"[ऀ-ॿ]", record.get("house_no", "")):
                record["house_no"] = paddle_house
            elif e_digit and not re.search(r"[ऀ-ॿ]", record.get("house_no", "")):
                record["house_no"] = e_digit
            elif parser_house:
                record["house_no"] = parser_house
        except Exception as exc:
            log.debug("House number extraction failed: %s", exc)

        # Defer ALL PaddleOCR calls to the main thread — not thread-safe even
        # with a lock. Store crops AND current Tesseract values so the
        # sequential pass can skip PaddleOCR when Tesseract is already good.
        record["_cl_for_deleted"] = cl
        record["_serial_png_for_deleted"] = serial_png
        record["_hv_png_for_paddle"] = hv_png
        record["_age_png_for_paddle"] = _crop_png(cl, AGE_REGION, "AGE_DEFER") if PaddleOCR is not None else None
        record["_tess_house"] = record.get("house_no", "")
        # Digit runs from the wider house-band read, for the free-text merge.
        # That read often keeps a digit the value crop lost to the bracket this
        # model renders a printed '1' as, and it is the only evidence that can
        # place a leading digit which left no run of its own to align on. The
        # main thread needs it because the merge runs there, after the pool.
        corroborating: list[str] = []
        for band in (h_lines, hv_lines):
            for line in (band or []):
                cleaned = _LOOKALIKE_RUN.sub("1", _house_line([line]))
                corroborating.extend(re.findall(r"\d+", cleaned))
        record["_house_corroborating"] = corroborating
        record["_tess_age"] = record.get("age", "")
        record["is_deleted"] = False  # filled in after thread pool

        # ── 9. Final record ──────────────────────────────────────────────
        if not any(record.get(k) for k in ("sno", "id_card_no", "voter_first_name", "age", "is_deleted")):
            _t("FINAL RECORD", "→ discarded (no usable fields)")
            return None

        _t("FINAL RECORD", record)
        return record

    except Exception as exc:
        log.debug("Card extraction failed: %s", exc)
        return None



def _resolve_page_range(
    page_count: int,
    start_page: Optional[int],
    end_page: Optional[int],
    whole_pdf: bool,
) -> tuple[int, int, str]:
    """Validate and resolve page range. Returns (start, end, mode). Raises ValueError for invalid inputs."""
    if whole_pdf:
        if start_page is not None or end_page is not None:
            raise ValueError("whole_pdf=true cannot be combined with start_page or end_page")
        return 1, page_count, "whole_pdf"
    if start_page is None:
        raise ValueError("start_page is required unless whole_pdf=true")
    if start_page < 1:
        raise ValueError("start_page must be >= 1")
    selected_end = start_page if end_page is None else end_page
    if selected_end < start_page:
        raise ValueError("end_page must be greater than or equal to start_page")
    if selected_end > page_count:
        raise ValueError(f"end_page {selected_end} exceeds PDF page count {page_count}")
    return start_page, selected_end, "page_range"

def _safe_filename(filename: str, default: str = "document.pdf") -> str:
    """Sanitize filename by removing invalid chars and handling reserved Windows names. Returns safe filename or default."""
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
    """Return sanitized PDF filename."""
    return _safe_filename(filename, "document.pdf")

def _split_person_name(full_name: str) -> tuple[str, str, str]:
    """Split full name into (first, middle, last). Returns empty strings for missing parts."""
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
    page_number = record.get("_page_number")
    for relation in ("husband", "father", "mother", "other"):
        first, middle, last = _split_person_name(_apply_name_corrections(
            record.get(f"voter_{relation}_name", ""), page_number))
        relation_parts[f"voter_{relation}_first_name"] = first
        relation_parts[f"voter_{relation}_middle_name"] = middle
        relation_parts[f"voter_{relation}_last_name"] = last

    voter_name = _apply_name_corrections(
        " ".join(filter(None, [
            record.get("voter_first_name", ""),
            record.get("voter_middle_name", ""),
            record.get("voter_sur_name", ""),
        ])),
        page_number,
    )
    vf, vm, vs = _split_person_name(voter_name)

    return {
        "sno": counter,
        # Page provenance. The consumer keys record identity on (page, row) and
        # rejects duplicates, so without this a multi-page unit has no way to
        # tell which page a record came from and files them all under the first.
        # Prefixed with "_" to match the rest of this response's private fields
        # (_needs_review, _review_reasons, _field_sources).
        "page_number": record.get("_page_number"),
        "card_index": record.get("_card_index"),
        "voter_sr_no": str(record.get("sno", "")),
        "id_card_no": record.get("id_card_no", ""),
        "gender": record.get("gender", ""),
        "age": record.get("age", ""),
        "house_no": record.get("house_no", ""),
        "voter_first_name": vf,
        "voter_middle_name": vm,
        "voter_sur_name": vs,
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

def _page_looks_like_voter_grid(page: fitz.Page) -> bool:
    """Probe representative cards to detect voter grid. Returns True if EPIC found in any probed position."""
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

def _extract_state_code(page: fitz.Page) -> str:
    """Extract two-digit state code from page-1 S24 heading using OCR. Returns empty string on failure."""
    try:
        header_height = min(65.0, page.rect.height)
        header_clip = fitz.Rect(
            page.rect.x0, page.rect.y0,
            page.rect.x1, page.rect.y0 + header_height,
        )
        image_bytes = getattr(page, "get_pixmap")(
            dpi=300, clip=header_clip, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return ""
        height, width = image.shape[:2]
        heading = image[
            int(height * 0.50):int(height * 0.92),
            int(width * 0.48):int(width * 0.60),
        ]
        heading = cv2.resize(heading, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(heading, cv2.COLOR_BGR2GRAY)
        txt = pytesseract.image_to_string(Image.fromarray(gray), lang="eng", config="--oem 3 --psm 7")
        match = re.search(r"[S$]\s*(\d{2})", txt.upper())
        return match.group(1) if match else ""
    except Exception as exc:
        log.warning("State-code extraction failed: %s", exc)
        return ""

def _extract_roll_header_metadata(page: fitz.Page) -> dict[str, str]:
    """Extract roll-wide codes (ac, anubhag, booth) and Hindi section name from page 3 header. Returns empty dict on failure."""
    empty = {
        "state_code": "",
        "ac_code": "",
        "anubhag_code": "",
        "anubhag_name": "",
        "booth_code": "",
    }
    try:
        header_height = min(40.0, page.rect.height)
        header_clip = fitz.Rect(
            page.rect.x0, page.rect.y0,
            page.rect.x1, page.rect.y0 + header_height,
        )
        image_bytes = getattr(page, "get_pixmap")(
            dpi=300, clip=header_clip, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return empty
        height, width = image.shape[:2]

        def crop(x0: float, y0: float, x1: float, y1: float, scale: float = 3) -> Any:
            region = image[
                int(height * y0):int(height * y1),
                int(width * x0):int(width * x1),
            ]
            return cv2.resize(region, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        def header_number(region: Any) -> str:
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
            t = pytesseract.image_to_string(Image.fromarray(gray), lang="eng", config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789").strip()
            if t:
                return t
            # Tesseract digit-only config misses some crops — try PaddleOCR and
            # extract the first run of digits from whatever it returns.
            if PaddleOCR is not None:
                try:
                    _paddle = _get_paddle()
                    result = _paddle.ocr(region, cls=False)
                    raw = " ".join(
                        line[1][0] for line in (result[0] or []) if line[1][0]
                    )
                    m = re.search(r"\d+", raw)
                    if m:
                        return m.group(0)
                except Exception:
                    pass
            return ""

        ac_code = header_number(crop(0.282, 0.06, 0.363, 0.39))
        booth_code = header_number(crop(0.847, 0.06, 0.984, 0.39))

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
        # anubhag_code is the leading digit before the first "-" in the text,
        # e.g. " 1-उत्तरांचल कालोनी..." -> "1". More reliable than a narrow crop.
        anubhag_code_match = re.match(r"\s*(\d+)\s*-", anubhag_text)
        anubhag_code = anubhag_code_match.group(1) if anubhag_code_match else ""
        # Normalize Devanagari digits (०-९ = ०-९) to ASCII
        anubhag_code = anubhag_code.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        anubhag_name = re.sub(r"^[\s|:：;,.\-–—\d०-९]+", "", anubhag_text).strip()
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

MAX_PDF_BYTES = int(os.getenv("MAX_OCR_PDF_BYTES", str(100 * 1024 * 1024)))
MAX_BATCH_FILES = int(os.getenv("MAX_OCR_BATCH_FILES", "100"))
MAX_BATCH_BYTES = int(os.getenv("MAX_OCR_BATCH_BYTES", str(1024 * 1024 * 1024)))
BATCH_WORK_DIR = Path(os.getenv("OCR_BATCH_WORK_DIR", "/tmp/ocr_pdf_jobs")).expanduser().resolve()
OCR_OUTPUT_DIR = Path(os.getenv("OCR_OUTPUT_DIR", str(Path(__file__).parent.parent / "results"))).expanduser().resolve()



app = FastAPI(
    title="Electoral Roll OCR API",
    version="1.0.0",
    description="Electoral Roll OCR",
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
    allow_origins=list(_cors_origins) if _cors_origins else ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"] if _cors_origins else [],
)
_OCR_LOCK = asyncio.Lock()

def _extract_pdf_ocr_unlocked(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    whole_pdf: bool = False,
    skip_non_voter_pages: bool = True,
) -> dict[str, Any]:
    """Extract OCR from PDF without lock. Processes pages, extracts voter cards, reconciles serials. Returns results dict."""
    started = time.perf_counter()
    source_pdf_name = _source_pdf_name(filename)
    _progress_set_phase("request")
    _progress(
        f"request_start file={source_pdf_name} bytes={len(pdf_bytes):,} "
        f"range={start_page or 1}-{end_page or 'end'} whole_pdf={whole_pdf}",
        force=True,
    )
    print(f"[OCR] request_start file={source_pdf_name} bytes={len(pdf_bytes)}", flush=True)
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
        first, last, mode = _resolve_page_range(page_count, start_page, end_page, whole_pdf)
        print(f"[OCR] selection mode={mode} pages={first}-{last} skip_non_voter_pages={skip_non_voter_pages}", flush=True)

        metadata_page_number = 3 if page_count >= 3 else 1
        metadata_started = time.perf_counter()
        roll_metadata = _extract_roll_header_metadata(document[metadata_page_number - 1])
        roll_metadata["state_code"] = _extract_state_code(document[0])
        print(f"[OCR] header_metadata page={metadata_page_number} elapsed={time.perf_counter() - metadata_started:.2f}s", flush=True)

        records: list[dict[str, Any]] = []
        processed_pages: list[dict[str, Any]] = []
        skipped_pages: list[dict[str, Any]] = []
        serial_corrections: list[dict[str, Any]] = []

        for page_number in range(first, last + 1):
            page = document[page_number - 1]
            probe_started = time.perf_counter()
            _progress_set_phase("probe")
            _progress(f"grid probe on {len(_voter_card_rects(page))} card positions", force=True)
            looks_like_voter_grid = (
                _page_looks_like_voter_grid(page)
                if skip_non_voter_pages else True
            )
            probe_elapsed = time.perf_counter() - probe_started
            print(f"[OCR] page={page_number} grid_probe={probe_elapsed:.2f}s voter_grid={looks_like_voter_grid}", flush=True)
            if not looks_like_voter_grid:
                _progress(f"page {page_number} SKIPPED (not a voter grid)", force=True)
                skipped_pages.append({
                    "page_number": page_number,
                    "reason": "no EPIC found in the expected card grid positions",
                })
                continue

            page_started = time.perf_counter()
            page_records: list[dict[str, Any]] = []
            card_rects = _voter_card_rects(page)
            render_started = time.perf_counter()
            try:
                # Render the full page once at 200 DPI and slice card crops from
                # the resulting NumPy array. This replaces ~30 individual
                # page.get_pixmap() calls (one per card) with a single render.
                full_pix = page.get_pixmap(dpi=200, alpha=False) # type: ignore
                full_arr = np.frombuffer(full_pix.samples, np.uint8).reshape(
                    full_pix.height, full_pix.width, full_pix.n
                )
                pw, ph = page.rect.width, page.rect.height
                rendered_cards = []
                for rect in card_rects:
                    x0 = max(0, int(rect.x0 / pw * full_pix.width))
                    y0 = max(0, int(rect.y0 / ph * full_pix.height))
                    x1 = min(full_pix.width, int(rect.x1 / pw * full_pix.width))
                    y1 = min(full_pix.height, int(rect.y1 / ph * full_pix.height))
                    crop = full_arr[y0:y1, x0:x1]
                    ok, enc = cv2.imencode(".png", crop)
                    rendered_cards.append(enc.tobytes() if ok else None)
            except Exception as _render_exc:
                log.warning("Full-page render failed, falling back to per-card: %s", _render_exc)
                rendered_cards = None
            render_elapsed = time.perf_counter() - render_started
            _progress(f"full page rendered at 200 DPI in {render_elapsed:.2f}s "
                      f"({'sliced' if rendered_cards else 'per-card fallback'})", force=True)
            print(f"[OCR] page={page_number} full_page_render={render_elapsed:.2f}s mode={'sliced' if rendered_cards else 'fallback'}", flush=True)

            def extract_card_at(index_and_rect: tuple[int, fitz.Rect]) -> Optional[dict[str, Any]]:
                index, rect = index_and_rect
                card_started = time.perf_counter()
                try:
                    rendered = rendered_cards[index - 1] if rendered_cards else None
                    rec = _extract_card(page, rect, page_number, index, rendered)
                    _progress(
                        f"card {index:2d}/{len(card_rects)} done in "
                        f"{time.perf_counter() - card_started:5.2f}s"
                        + ("" if rec else " (no record)")
                    )
                    return rec
                except Exception as _card_exc:
                    _progress(f"card {index:2d}/{len(card_rects)} FAILED in "
                              f"{time.perf_counter() - card_started:5.2f}s: "
                              f"{type(_card_exc).__name__}: {_card_exc}", force=True)
                    raise

            indexed_rects = list(enumerate(card_rects, 1))
            worker_count = _OCR_CARD_WORKERS
            _progress_set_phase("cards")
            _progress(f"starting {len(card_rects)} card threads "
                      f"(workers={worker_count})", force=True)
            if worker_count > 1:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    extracted_records = list(executor.map(extract_card_at, indexed_rects))
            else:
                extracted_records = [extract_card_at(item) for item in indexed_rects]
            _progress(f"all {len(card_rects)} cards extracted", force=True)

            # ── Sequential PaddleOCR pass (not thread-safe) ──────────────
            # Per-card paddle calls for house+age. Deleted uses Q-in-serial only.
            paddle_started = time.perf_counter()
            _progress_set_phase("paddle")
            _progress("loading paddle model (cached across pages)" if _paddle_singleton
                      else "loading paddle model (first call, slow)", force=True)
            paddle_inst = _get_paddle() if PaddleOCR is not None else None
            _progress(f"paddle ready in {time.perf_counter() - paddle_started:.2f}s", force=True)

            paddle_done = 0
            for rec in extracted_records:
                if rec is None:
                    continue
                cl_img = rec.pop("_cl_for_deleted", None)
                ser_png = rec.pop("_serial_png_for_deleted", None)
                hv_png_deferred = rec.pop("_hv_png_for_paddle", None)
                age_png_deferred = rec.pop("_age_png_for_paddle", None)
                tess_house = rec.pop("_tess_house", "")
                tess_age = rec.pop("_tess_age", "")

                if paddle_inst is None:
                    rec["is_deleted"] = False
                    paddle_done += 1
                    continue

                tess_age_ok = bool(tess_age and tess_age.isdigit()
                                   and len(tess_age) >= 2 and int(tess_age) >= 18)

                # One combined paddle call: serial (for Q/deleted) + house + age
                # Stacked vertically with known pixel offsets for result routing.
                try:
                    crops_info = []  # (field, scaled_img)

                    if ser_png:
                        nparr = np.frombuffer(ser_png, np.uint8)
                        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            s = min(6, max(2, 80 // max(1, img.shape[0])))
                            crops_info.append(("serial", cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)))

                    if hv_png_deferred:
                        nparr = np.frombuffer(hv_png_deferred, np.uint8)
                        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            s = min(6, max(2, 80 // max(1, img.shape[0])))
                            crops_info.append(("house", cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)))

                    if age_png_deferred and not tess_age_ok:
                        nparr = np.frombuffer(age_png_deferred, np.uint8)
                        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            s = min(6, max(2, 80 // max(1, img.shape[0])))
                            crops_info.append(("age", cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)))

                    rec["is_deleted"] = False
                    if crops_info:
                        max_w = max(c.shape[1] for _, c in crops_info)
                        padded = []
                        h_offsets = {}
                        y = 0
                        for field, c in crops_info:
                            pad = cv2.copyMakeBorder(c, 4, 4, 4, max_w - c.shape[1] + 4,
                                                      cv2.BORDER_CONSTANT, value=(255,255,255))
                            h_offsets[field] = (y, y + pad.shape[0])
                            y += pad.shape[0]
                            padded.append(pad)
                        stacked = np.vstack(padded) if len(padded) > 1 else padded[0]
                        total_h = stacked.shape[0]

                        res = paddle_inst.ocr(stacked, cls=False)
                        hits = res[0] or []

                        for field, (fy0, fy1) in h_offsets.items():
                            field_texts = [
                                ln[1][0] for ln in hits
                                if fy0 <= sum(pt[1] for pt in ln[0]) / 4 <= fy1
                            ]

                            if field == "serial":
                                if any("Q" in t.upper() for t in field_texts):
                                    rec["is_deleted"] = True

                            elif field == "house" and field_texts:
                                paddle_house = None
                                for pt in field_texts:
                                    mf = re.search(r"(\d{1,5})\s*/\s*(\d{1,4})", pt)
                                    if mf:
                                        paddle_house = f"{mf.group(1)}/{mf.group(2)}"
                                        break
                                if not paddle_house:
                                    all_runs = []
                                    for pt in field_texts:
                                        all_runs.extend(re.findall(r"\d+", pt))
                                    if all_runs:
                                        paddle_house = max(all_runs, key=len)
                                if paddle_house:
                                    cur = rec.get("house_no", "")
                                    has_deva = re.search(r"[ऀ-ॿ]", cur)
                                    if not has_deva:
                                        rec["house_no"] = paddle_house
                                    elif has_deva:
                                        # Digit runs stashed by the card thread
                                        # from Tesseract's wider house-band read.
                                        merged = _merge_text_with_digits(
                                            cur, paddle_house, field_texts,
                                            rec.pop("_house_corroborating", None),
                                        )
                                        if merged:
                                            rec["house_no"] = merged
                                        else:
                                            # Mixed value like '5 ए' — if paddle got
                                            # a leading-1 fix (15 vs 5), prepend 1
                                            cur_digits = re.sub(
                                                r"[^\d]", "", cur.split()[0]) if cur else ""
                                            if cur_digits and paddle_house == "1" + cur_digits:
                                                rec["house_no"] = "1" + cur

                            elif field == "age" and field_texts:
                                paddle_ages = []
                                for pt in field_texts:
                                    for digits in re.findall(r"\d{1,3}", pt):
                                        if 18 <= int(digits) <= 120:
                                            paddle_ages.append(digits)
                                if paddle_ages:
                                    rec["age"] = max(paddle_ages, key=lambda x: (len(x), int(x)))
                    else:
                        rec["is_deleted"] = False

                except Exception as _pe:
                    log.debug("PaddleOCR combined pass failed: %s", _pe)
                    rec["is_deleted"] = _detect_deleted_watermark(cl_img, serial_png=ser_png)

                # Danda/double-danda lookalike fix
                house_val = rec.get("house_no", "")
                if house_val:
                    house_val = re.sub(r"^[।॥|]+(?=\d)", "1", house_val)
                    house_val = re.sub(r"(?<=\d)[।॥|]+(?=\d)", "1", house_val)
                    rec["house_no"] = house_val

                paddle_done += 1
                _progress(
                    f"paddle {paddle_done:2d}/{len(extracted_records)} "
                    f"house={rec.get('house_no', '')!r} age={rec.get('age', '')!r} "
                    f"deleted={rec.get('is_deleted', False)} "
                    f"[+{time.perf_counter() - paddle_started:.1f}s]"
                )

            print(f"[OCR] page={page_number} paddle_sequential={time.perf_counter()-paddle_started:.2f}s", flush=True)
            _progress(f"paddle pass done in {time.perf_counter()-paddle_started:.2f}s", force=True)

            for (card_index, _), record in zip(indexed_rects, extracted_records):
                if record is None:
                    continue
                record["_page_number"] = page_number
                record["_card_index"] = card_index
                page_records.append(record)

            records.extend(page_records)
            processed_pages.append({"page_number": page_number, "records": len(page_records)})
            page_elapsed = time.perf_counter() - page_started
            _progress(f"page {page_number} COMPLETE cards={len(card_rects)} "
                      f"records={len(page_records)} in {page_elapsed:.2f}s", force=True)
            print(f"[OCR] page={page_number} complete cards={len(card_rects)} records={len(page_records)} elapsed={page_elapsed:.2f}s", flush=True)

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
    _progress_set_phase("request")
    _progress(
        f"request COMPLETE pages={len(processed_pages)} skipped={len(skipped_pages)} "
        f"records={len(public_records)} in {elapsed:.2f}s",
        force=True,
    )
    print(f"[OCR] request_complete pages_processed={len(processed_pages)} pages_skipped={len(skipped_pages)} records={len(public_records)} elapsed={elapsed:.2f}s", flush=True)
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
        "records_needing_review": sum(record["needs_review"] for record in public_records),
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
    """Extract OCR from PDF. Calls _extract_pdf_ocr_unlocked directly (no file lock, stateless)."""
    return _extract_pdf_ocr_unlocked(
        pdf_bytes,
        filename,
        start_page,
        end_page,
        whole_pdf,
        skip_non_voter_pages,
    )

def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    """Write JSON to file atomically using temporary file to avoid corruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint returning OCR engine status, availability, and configuration limits."""
    return {
        "status": "ok",
        "engine": "tesseract" + ("+paddleocr" if PaddleOCR is not None else ""),
        "busy": _OCR_LOCK.locked(),
        "max_pdf_mb": MAX_PDF_BYTES // (1024 * 1024),
        "paddleocr_available": PaddleOCR is not None,
        "batch_max_files": MAX_BATCH_FILES,
    }

@app.post("/ocr/extract")
async def ocr_extract_endpoint(
    pdf_file: UploadFile = File(..., description="Electoral-roll PDF"),
    start_page: Optional[int] = Form(default=None, description="First page, 1-based and inclusive"),
    end_page: Optional[int] = Form(default=None, description="Last page, 1-based and inclusive"),
    whole_pdf: bool = Form(default=False, description="Process the complete PDF; cannot be combined with a range"),
    skip_non_voter_pages: bool = Form(default=True, description="Skip pages without EPICs in the expected card grid"),
) -> Response:
    """Extract OCR from uploaded PDF. Validates file, processes pages, returns JSON results with voter records."""
    filename = pdf_file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported")

    raw = await pdf_file.read(MAX_PDF_BYTES + 1)
    if not raw:
        raise HTTPException(400, "PDF file is empty")
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit")

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
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).stem).strip(" .") or "document"
        output_path = OCR_OUTPUT_DIR / f"{uuid.uuid4().hex}_{safe_stem}_ocr.json"
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



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("OCR_API_HOST", "0.0.0.0"),
        port=int(os.getenv("OCR_API_PORT", "8082")),
    )
