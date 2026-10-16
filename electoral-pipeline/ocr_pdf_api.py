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
import copy
import hashlib
import json
import logging
import os
import re
import shlex
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
import httpx
import numpy as np
import pytesseract
from PIL import Image
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

try:
    from paddleocr import PaddleOCR
except Exception as exc:
    # Not just ImportError. On Windows the paddleocr package can load and then
    # fail inside torch's own DLL loading with OSError (WinError 127, shm.dll),
    # which escapes an `except ImportError` entirely and takes the whole module
    # down with it -- including /health, since FastAPI never finishes importing.
    # PaddleOCR=None is an already-supported state: every call site either
    # checks for it or catches Exception, so degrading here is correct rather
    # than a new failure mode.
    log = logging.getLogger(__name__)
    log.warning("paddleocr unavailable (%s: %s); falling back to Tesseract-only",
                type(exc).__name__, exc)
    PaddleOCR = None

log = logging.getLogger(__name__)

import threading
_PADDLE_LOCK = threading.Lock()
_paddle_singleton = None

# A document is sent once for every page-range unit.  Cover/header OCR is
# document-wide, deterministic work, so repeating it for every three-page
# request added ~8 seconds per unit without improving any record.  Cache only
# completed metadata and key it by the *bytes* of the PDF (not its filename):
# a different upload with the same name can never inherit metadata.  The OCR
# endpoint serialises extraction under _OCR_LOCK, so this small in-process
# LRU needs no additional locking.
_ROLL_METADATA_CACHE_MAX = max(1, int(os.getenv("OCR_METADATA_CACHE_DOCUMENTS", "64")))
_roll_metadata_cache: dict[str, dict[str, Any]] = {}
_roll_metadata_cache_order: list[str] = []


def _cache_roll_metadata(pdf_bytes: bytes, metadata: dict[str, Any]) -> None:
    key = hashlib.sha256(pdf_bytes).hexdigest()
    if key in _roll_metadata_cache_order:
        _roll_metadata_cache_order.remove(key)
    _roll_metadata_cache[key] = copy.deepcopy(metadata)
    _roll_metadata_cache_order.append(key)
    while len(_roll_metadata_cache_order) > _ROLL_METADATA_CACHE_MAX:
        expired = _roll_metadata_cache_order.pop(0)
        _roll_metadata_cache.pop(expired, None)


def _cached_roll_metadata(pdf_bytes: bytes) -> dict[str, Any] | None:
    key = hashlib.sha256(pdf_bytes).hexdigest()
    value = _roll_metadata_cache.get(key)
    if value is None:
        return None
    _roll_metadata_cache_order.remove(key)
    _roll_metadata_cache_order.append(key)
    return copy.deepcopy(value)

def _get_paddle():
    """Return a shared PaddleOCR instance, initializing it once on first call."""
    global _paddle_singleton
    if _paddle_singleton is not None:
        return _paddle_singleton
    with _PADDLE_LOCK:
        if _paddle_singleton is None:
            _use_gpu = os.environ.get("PADDLE_USE_GPU", "").lower() in ("1", "true", "yes")
            _paddle_singleton = PaddleOCR(
                use_angle_cls=False, lang="en", show_log=False, use_gpu=_use_gpu
            )  # type: ignore
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

# Tesseract call counters. pytesseract spawns a subprocess per call, so the
# call count -- not the pixel count -- is what sets the floor on runtime: each
# launch re-deserialises the model. These counters exist to measure that, and to
# prove the persistent-engine work actually removed launches rather than merely
# moving them around. Pure instrumentation: nothing here changes a result.
_TESS_CALLS: Counter = Counter()
_TESS_CALL_LOCK = threading.Lock()


def _tess_count(kind: str, n: int = 1) -> None:
    """Record n Tesseract launches of the given kind."""
    if not _TESS_INSTRUMENT:
        return
    with _TESS_CALL_LOCK:
        _TESS_CALLS[kind] += n


def _tess_counters_snapshot() -> dict[str, int]:
    with _TESS_CALL_LOCK:
        return dict(_TESS_CALLS)


def _tess_counters_reset() -> None:
    with _TESS_CALL_LOCK:
        _TESS_CALLS.clear()


# Card worker pool. Created lazily on first use and reused for the life of the
# process, so a thread's cached Tesseract engine survives from one page to the
# next instead of being rebuilt 30 times a page.
#
# Concurrency is bounded by _OCR_LOCK (one extraction at a time), so this pool
# never has two requests competing for its workers. Do NOT call shutdown() per
# page: shutdown is terminal, and a shut-down executor refuses the next map.
# Callers simply consume the map iterator, which blocks until the page's cards
# are done.
_CARD_POOL: Optional[ThreadPoolExecutor] = None
_CARD_POOL_LOCK = threading.Lock()


def _card_pool() -> ThreadPoolExecutor:
    global _CARD_POOL
    with _CARD_POOL_LOCK:
        if _CARD_POOL is None:
            _CARD_POOL = ThreadPoolExecutor(
                max_workers=_OCR_CARD_WORKERS,
                thread_name_prefix="ocr-card",
            )
        return _CARD_POOL


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


# ── Card progress counter (in-memory, read-only) ────────────────────────────
# Separate from the stdout logger above on purpose. Logging is line-oriented
# and lossy by design: _PROGRESS_MIN_INTERVAL can drop lines, and the
# heartbeat interleaves with real work. A counter needs to be exact, because
# the UI presents it as "N cards scanned" and an undercount reads as a stall.
#
# Read-only and observational. Nothing here is consulted by the extraction
# path: the counter is incremented strictly *after* a card has finished, so
# it cannot influence what any engine produces. Reset per request, never
# persisted, so a stale count cannot leak into a later run.

_PROGRESS_SNAPSHOT: dict[str, Any] = {
    "cards_done": 0,
    "pages_done": 0,
    "cards_total": 0,
    "pages_total": 0,
    "cards_records": 0,
    "started_at": 0.0,
    "updated_at": 0.0,
    "active_page": None,
    "done": False,
}


def _progress_reset(pages_total: int = 0) -> None:
    """Zero the counter at the start of a request."""
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT.update({
            "cards_done": 0,
            "pages_done": 0,
            "cards_total": 0,
            "pages_total": int(pages_total or 0),
            "cards_records": 0,
            "started_at": now_ts,
            "updated_at": now_ts,
            "active_page": None,
            "done": False,
        })


def _progress_card_done(*, page_number: int, records: int) -> None:
    """One card finished. Increments only; never touches a record."""
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["cards_done"] += 1
        _PROGRESS_SNAPSHOT["cards_records"] += max(0, int(records or 0))
        _PROGRESS_SNAPSHOT["active_page"] = page_number
        _PROGRESS_SNAPSHOT["updated_at"] = now_ts


def _progress_page_done(page_number: int, cards_on_page: int) -> None:
    """One page finished. Adds to the known card total, so `cards_total` is a
    denominator the UI can divide by once the first page lands."""
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["pages_done"] += 1
        _PROGRESS_SNAPSHOT["cards_total"] += max(0, int(cards_on_page or 0))
        _PROGRESS_SNAPSHOT["active_page"] = page_number
        _PROGRESS_SNAPSHOT["updated_at"] = now_ts


def _progress_finish() -> None:
    """Mark the run complete. Counts are already final; this only stops the
    UI projecting a moving number for work that has ended."""
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["done"] = True
        _PROGRESS_SNAPSHOT["updated_at"] = now_ts


def _progress_snapshot() -> dict[str, Any]:
    """A copy of the counter, safe to serialise. Never raises.

    A copy rather than the live dict on purpose: handing out the real object
    would let a serialiser mutate the counter's internals, or let a caller
    hold a reference across a reset and keep writing into the next run.
    """
    with _PROGRESS_LOCK:
        snap = dict(_PROGRESS_SNAPSHOT)
    snap["elapsed_s"] = round(
        max(0.0, snap["updated_at"] - snap["started_at"]), 1
    ) if snap["started_at"] else 0.0
    snap["idle_s"] = round(
        max(0.0, time.time() - snap["updated_at"]), 1
    ) if snap["updated_at"] else 0.0
    return snap


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
    # Full-roll run (2026-10-03, 531 records). The मात्रा + ह cluster in सिंह
    # is the one recurring misread: सिंह renders as सिंड / सिंय. Both bad
    # tokens are verified absent from the ground-truth corpus (सिंह = 28,
    # सिंड = 0, सिंय = 0), so neither rule can shadow a correct name.
    # Frequency for context: सिंह x161, सिंड x1, सिंय x1.
    "सिंड":         "सिंह",        # ह↔ड
    "सिंय":         "सिंह",        # ह↔य
    # Verified against the rendered pixels of the card, not against a second
    # OCR pass. Each bad token is checked absent from every correct name in the
    # roll, so no rule here can shadow a real name elsewhere.
    "राख्व्वी":      "राखी",        # व्व cluster split out of ख (p16 c21)
    "श्रर्मा":       "शर्मा",        # spurious ्र before र्म (p18 c19)
    "वब्रजवाला":     "ब्रजवाला",     # spurious व prefix (p15 c11)
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

# Tesseract launch counting. On by default so a benchmark run cannot silently
# skip it; the counter costs one dict increment under a lock, which is noise
# next to a subprocess launch.
_TESS_INSTRUMENT = os.getenv("OCR_TESS_COUNTERS", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

# ── Tesseract backend ────────────────────────────────────────────────────────
# pytesseract forks a `tesseract` process per read, and every launch
# re-deserialises the model (~150 ms here against ~1 ms for a warm in-process
# engine). tesserocr keeps the engine loaded, so a long-lived worker thread
# pays that cost once.
#
#   pytesseract  one subprocess per call (the original behaviour; the default)
#   tesserocr    one cached in-process engine per (thread, language)
#   shadow       runs BOTH on every call, returns pytesseract's answer, and
#                counts any disagreement. For verification, not production.
#
# The call sites keep their (image, lang=, config=) shape and the config
# strings they already use, so there is one definition of "what a read is".
try:
    import tesserocr
except Exception as _tesserocr_exc:  # ImportError, or a missing shared library
    tesserocr = None
    _TESSEROCR_IMPORT_ERROR: Optional[str] = (
        f"{type(_tesserocr_exc).__name__}: {_tesserocr_exc}")
else:
    _TESSEROCR_IMPORT_ERROR = None

_TESS_BACKEND_REQUESTED = os.getenv("OCR_TESS_BACKEND", "pytesseract").strip().lower()
if _TESS_BACKEND_REQUESTED not in {"pytesseract", "tesserocr", "shadow"}:
    raise ValueError(
        f"OCR_TESS_BACKEND={_TESS_BACKEND_REQUESTED!r}: expected "
        f"pytesseract, tesserocr or shadow")
_TESS_BACKEND = _TESS_BACKEND_REQUESTED
if _TESS_BACKEND != "pytesseract" and tesserocr is None:
    # Degrade rather than refuse to import: a module that cannot import takes
    # /health down with it. But never quietly -- /health reports both the
    # requested and the effective backend, so this cannot masquerade as success.
    logging.getLogger(__name__).warning(
        "OCR_TESS_BACKEND=%s requested but tesserocr is unavailable (%s); "
        "falling back to pytesseract", _TESS_BACKEND, _TESSEROCR_IMPORT_ERROR)
    _TESS_BACKEND = "pytesseract"

_TESS_LOCAL = threading.local()
_TESS_SHADOW_LOCK = threading.Lock()
_TESS_SHADOW_SAMPLES: list[dict[str, Any]] = []
_TESS_SHADOW_SAMPLE_LIMIT = 25


def _tess_backend_info() -> dict[str, Any]:
    return {
        "requested": _TESS_BACKEND_REQUESTED,
        "effective": _TESS_BACKEND,
        "tesserocr_available": tesserocr is not None,
        "tesserocr_error": _TESSEROCR_IMPORT_ERROR,
    }


def _tess_shadow_snapshot() -> list[dict[str, Any]]:
    with _TESS_SHADOW_LOCK:
        return list(_TESS_SHADOW_SAMPLES)


def _tess_shadow_reset() -> None:
    with _TESS_SHADOW_LOCK:
        _TESS_SHADOW_SAMPLES.clear()


def _tess_shadow_note(kind: str, lang: str, config: str, py: Any, tc: Any) -> None:
    _tess_count("shadow_mismatch")
    _tess_count(f"shadow_mismatch_{kind}")
    with _TESS_SHADOW_LOCK:
        if len(_TESS_SHADOW_SAMPLES) < _TESS_SHADOW_SAMPLE_LIMIT:
            _TESS_SHADOW_SAMPLES.append({
                "kind": kind, "lang": lang, "config": config,
                "pytesseract": repr(py)[:300], "tesserocr": repr(tc)[:300],
            })


_TESS_CONFIG_CACHE: dict[str, tuple[int, int, tuple[tuple[str, str], ...]]] = {}


def _parse_tess_config(config: str) -> tuple[int, int, tuple[tuple[str, str], ...]]:
    """Split a pytesseract config string into (oem, psm, ((variable, value), ...)).

    Tokenised with shlex because pytesseract does exactly that: the quotes
    around TESSERACT_HOUSE_WHITELIST are what keep its embedded space inside
    one value. Anything unrecognised raises instead of being dropped -- a
    silently ignored option would make the two backends read differently with
    nothing to show for it.
    """
    cached = _TESS_CONFIG_CACHE.get(config)
    if cached is not None:
        return cached
    tokens = shlex.split(config)
    oem, psm = 3, 3  # the tesseract CLI defaults when the flag is absent
    variables: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        flag = tokens[i]
        if flag in ("--oem", "--psm", "-c") and i + 1 >= len(tokens):
            raise ValueError(f"tesseract option {flag!r} has no value in {config!r}")
        if flag == "--oem":
            oem = int(tokens[i + 1])
        elif flag == "--psm":
            psm = int(tokens[i + 1])
        elif flag == "-c":
            name, sep, value = tokens[i + 1].partition("=")
            if not sep:
                raise ValueError(f"-c needs name=value, got {tokens[i + 1]!r}")
            variables.append((name, value))
        else:
            raise ValueError(f"unsupported tesseract option {flag!r} in {config!r}")
        i += 2
    parsed = (oem, psm, tuple(variables))
    _TESS_CONFIG_CACHE[config] = parsed
    return parsed


class _TessEngine:
    """One loaded tesserocr engine, reconfigured per call.

    oem is fixed when the engine is built; page segmentation mode and
    variables are not, so one engine per (thread, language) serves every
    config. The price is that state could leak between reads with different
    configs, so each call sets the psm and the full variable set explicitly and
    puts back anything an earlier call changed.
    """

    def __init__(self, lang: str, oem: int) -> None:
        self.api = tesserocr.PyTessBaseAPI(lang=lang, oem=oem)
        self.defaults: dict[str, str] = {}   # variable -> value before we touched it
        self.applied: dict[str, str] = {}    # variable -> value currently set

    def configure(self, psm: int, variables: tuple[tuple[str, str], ...]) -> None:
        self.api.SetPageSegMode(psm)
        wanted = dict(variables)
        for name in list(self.applied):
            if name not in wanted:
                self.api.SetVariable(name, self.defaults[name])
                del self.applied[name]
        for name, value in wanted.items():
            if self.applied.get(name) == value:
                continue
            if name not in self.defaults:
                self.defaults[name] = self.api.GetVariableAsString(name) or ""
            if not self.api.SetVariable(name, value):
                raise RuntimeError(f"tesseract rejected variable {name}={value!r}")
            self.applied[name] = value

    def reset(self) -> None:
        # Each subprocess read started cold. Drop the image and results, and any
        # adaptive-classifier state, so one read cannot colour the next.
        self.api.Clear()
        self.api.ClearAdaptiveClassifier()


def _tess_engine(lang: str, oem: int) -> _TessEngine:
    engines = getattr(_TESS_LOCAL, "engines", None)
    if engines is None:
        engines = _TESS_LOCAL.engines = {}
    engine = engines.get((lang, oem))
    if engine is None:
        engine = engines[(lang, oem)] = _TessEngine(lang, oem)
        _tess_count("engine_init")
    return engine


def _tesserocr_string(image: Any, lang: str, config: str) -> str:
    oem, psm, variables = _parse_tess_config(config)
    engine = _tess_engine(lang, oem)
    engine.configure(psm, variables)
    try:
        engine.api.SetImage(image)
        return engine.api.GetUTF8Text()
    finally:
        engine.reset()


_TSV_HEADER = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
               "left\ttop\twidth\theight\tconf\ttext\n")


def _tesserocr_data(image: Any, lang: str, config: str) -> dict[str, list]:
    oem, psm, variables = _parse_tess_config(config)
    engine = _tess_engine(lang, oem)
    engine.configure(psm, variables)
    try:
        engine.api.SetImage(image)
        rows = engine.api.GetTSVText(0)
    finally:
        engine.reset()
    # GetTSVText returns the data rows only. The header line comes from the
    # CLI's TSV renderer, not from this call, so without it file_to_dict
    # promotes the first data row to column names and every lookup downstream
    # finds nothing. Caught by shadow mode: all 60 Hindi reads disagreed.
    tsv = _TSV_HEADER + rows
    # pytesseract's own TSV parser, not a copy: it truncates confidence with
    # int(float(...)) and pads a missing last cell, and the line-grouping code
    # downstream depends on getting precisely those values.
    return pytesseract.pytesseract.file_to_dict(tsv, "\t", -1)


def _tess_string(image: Any, lang: str, config: str) -> str:
    """Drop-in for pytesseract.image_to_string(image, lang=..., config=...)."""
    if _TESS_BACKEND == "pytesseract":
        return pytesseract.image_to_string(image, lang=lang, config=config)
    if _TESS_BACKEND == "tesserocr":
        return _tesserocr_string(image, lang, config)
    expected = pytesseract.image_to_string(image, lang=lang, config=config)
    try:
        actual = _tesserocr_string(image, lang, config)
    except Exception as exc:
        actual = f"<{type(exc).__name__}: {exc}>"
    if actual != expected:
        _tess_shadow_note("string", lang, config, expected, actual)
    return expected


def _tess_data(image: Any, lang: str, config: str) -> dict[str, list]:
    """Drop-in for pytesseract.image_to_data(..., output_type=Output.DICT)."""
    if _TESS_BACKEND == "pytesseract":
        return pytesseract.image_to_data(
            image, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    if _TESS_BACKEND == "tesserocr":
        return _tesserocr_data(image, lang, config)
    expected = pytesseract.image_to_data(
        image, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    try:
        actual = _tesserocr_data(image, lang, config)
    except Exception as exc:
        actual = {"error": f"{type(exc).__name__}: {exc}"}
    if actual != expected:
        _tess_shadow_note("data", lang, config, expected, actual)
    return expected

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
    # EPIC / voter ID, at the card's top-right. 0.69 cut off the state
    # letters of the older printed form ``UP/79/389/0654035``. 0.52 keeps
    # those letters and still starts clear of the serial box.
    (0.52, 0.02, 1.00, 0.23),
    # The Hindi field block: name, relation, house, gender, age.
    # A long house line runs toward the photo box (0.66). Stopping at 0.60
    # cut कलोनी down to कलो. 0.64 recovers that word; 0.66 starts reading
    # the photo edge and turns जी-242 into जी-2442.
    (0.02, 0.22, 0.64, 0.90),
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
# The age line continues to the right with ``लिंग : <value>``.  This wider
# read is only used when the main card parse did not find a gender.
# Bottom extended to 0.82 to catch cards where the age row sits slightly lower.
GENDER_AGE_REGION = (0.03, 0.59, 0.78, 0.82)
# Recovery reader for the age value, used only when the main parse dropped
# it.  The Hindi model collapses Devanagari digits into one glyph, so the
# age value is located by label position and re-read with the English
# digits-only model.  Every value is tuned for GENDER_AGE_REGION at 200 DPI.
AGE_LABEL_RE = re.compile(r"आयु|आवु|आय|आग|आग़|आगु|आगू|आयू")
GENDER_LABEL_RE = re.compile(r"लिंग|लग|लिग|लिं|लिंग़")
# तृतीय लिंग is printed on these rolls alongside पुरुष / महिला, so the reader
# has to recognise it or the row falls through to blank.  Both त are required:
# a single त plus the ृ/ु matra is far too common in real names (मैत्रेय,
# क्षत्रिय, शत्रिया, त्रियाशु all match it), while त..त..य does not.  The
# matras and the doubled त in तृत्तीय are tolerated because these are scans;
# the constant is checked against every distinct name_hi in the database by
# tests/test_gender_vocabulary.py.
THIRD_GENDER_RE = re.compile(r"त(?:[ृु]|्र)्?[ँं]*[ीिे]?[ँं]*(?:त्?[ँं]*)*त्?[ँं]*[ीिे]?[ँं]*य")
# Label boxes are read from an upscaled image, so divide coordinates back
# down by this factor to index into the original crop.
AGE_SPAN_UPSCALE = 3
# Padding around the label box, and the narrowest span still worth reading.
AGE_SPAN_PAD_PX = 2
AGE_MIN_SPAN_PX = 4
# The value crop is tiny, so upscale it hard and give Tesseract a margin.
AGE_DIGIT_UPSCALE = 4
AGE_DIGIT_BORDER_PX = 20
# psm 7 (single line) is the only reliable pass for the tight digit span.
# psm 8 and psm 13 produce hallucinated values (notably a spike at 21) when
# the crop is ambiguous, so they are excluded.  If psm 7 on the span fails,
# the caller falls back to the full age/gender band, also with psm 7.
AGE_DIGIT_CONFIGS = (
    "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=0123456789",
)
# Re-reads of the age/gender band, in the order they earn their place. The
# order is measured, not guessed: adaptive thresholding recovers gender values
# that a plain upscale cannot, because the scans have an uneven grey wash that
# swamps Tesseract's own binarisation. Each variant is upscaled, because the
# band is only ~30px tall and the Hindi glyphs need the extra detail.
GENDER_BAND_UPSCALE = 3
GENDER_BAND_CONFIGS = (
    "--oem 3 --psm 7 -l hin",
    "--oem 3 --psm 6 -l hin",
)
# Adaptive threshold window/block, and the sharpen kernel. Both are tuned on
# the blank-gender sample: the window has to exceed the width of the faint
# horizontal scan streaks or they survive as glyph strokes.
GENDER_BAND_ADAPT_WINDOW = 31
GENDER_BAND_ADAPT_C = 10
GENDER_BAND_SHARPEN_KERNEL = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
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
# while slowing each pass.
TESSERACT_HINDI_LANG = "hin"
# Serial and EPIC are Latin digits only, so the Hindi model is the wrong one
# for them -- it has no digit classes and returns what it can from the
# shapes. Pure `eng` reads them directly.
#
# Both are read by Tesseract only. PaddleOCR never touches the EPIC: the
# combined serial/house/age pass does not stack it. And Paddle must not be
# moved onto the EPIC -- a 300 DPI crop fed to _read_line_variants is
# downscaled back to its fixed 60/80/100/120px targets, which destroys the
# thin digit strokes. Measured on this roll: 300 DPI matched the stored value
# on 0 of 10 EPICs where 200 DPI matched 5 of 10.
#
# The mirror image applies to the Q deleted-marker, which IS Paddle's: giving
# it a 300 DPI crop does not help either, because that crop is then upscaled
# by `min(6, max(2, 80 // height))` to ~80px, and p16/sno410 read as '410' at
# both DPIs on that path. Do not re-attempt a DPI fix for it without a probe
# that reproduces through _extract_card itself.
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


def _extract_text_with_tesseract(img_bytes: bytes) -> tuple[Optional[list[str]], Optional[float]]:
    """Read a card image with Hindi Tesseract, returning lines and mean confidence.
    Image should be pre-cleaned; reads grayscale and Otsu variants, scores by Devanagari chars and labels.
    Returns (lines, mean_confidence) where confidence is 0-100 or None.
    """
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None, None

        # Upscaling helps Tesseract's LSTM see Devanagari strokes; it works on
        # an estimated character height, and at card scale that estimate is
        # small enough to lose the matras below the headline.
        img_resized = cv2.resize(img, (0, 0), fx=2, fy=2,
                                 interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2)

        candidates: list[tuple[list[str], float]] = []
        for variant in (gray, thresh):
            # Use image_to_data to get per-word confidence
            _tess_count("hindi_text")
            data = _tess_data(
                Image.fromarray(variant),
                lang=TESSERACT_HINDI_LANG,
                config=TESSERACT_CONFIG,
            )
            # Extract text lines and their confidences.
            #
            # The line identity is (block_num, par_num, line_num), NOT line_num
            # alone. Every one of those counters restarts at 1 in its own
            # region, so on a voter card each printed row is a fresh paragraph
            # whose first line is numbered 1. Grouping on line_num alone
            # therefore concatenates every row that starts at 1:
            #
            #   blk par line  word   text
            #     1   1   1     1    'नाम'
            #     1   1   1     4    'मेता'
            #     1   2   1     1    'पिता'      <- paragraph 2, line 1 again
            #     1   2   1     4    'राजकुमार'
            #
            # which produced 'नाम : सचिन मेता पिता का नाम: राजकुमार' as ONE
            # line and merged the voter's name into the relation field on 479
            # of 531 records. Adding par_num restores the rows exactly; the
            # three-part key reproduces image_to_string's lines verbatim.
            lines = []
            confs = []
            current_line = []
            current_line_confs = []
            last_line_key = None
            for i, text in enumerate(data['text']):
                line_key = (
                    data['block_num'][i],
                    data['par_num'][i],
                    data['line_num'][i],
                )
                conf = data['conf'][i]
                if text.strip():
                    if line_key != last_line_key and current_line:
                        lines.append(" ".join(current_line))
                        if current_line_confs:
                            confs.append(sum(current_line_confs) / len(current_line_confs))
                        current_line = []
                        current_line_confs = []
                    current_line.append(text.strip())
                    if conf >= 0:  # -1 means no confidence
                        current_line_confs.append(conf)
                    last_line_key = line_key
            if current_line:
                lines.append(" ".join(current_line))
                if current_line_confs:
                    confs.append(sum(current_line_confs) / len(current_line_confs))

            if lines:
                mean_conf = sum(confs) / len(confs) if confs else 0.0
                candidates.append((lines, mean_conf))

        if not candidates:
            return None, None

        # Select best by hindi line score, return its lines and confidence.
        #
        # Confidence is the tie-breaker, and it has to be: `max` alone returns
        # the FIRST maximal element, so when two variants (gray/thresholded)
        # read the same Hindi text with equal score, the winner was decided by
        # dictionary order. That made the reported confidence an artefact of
        # which read happened to be considered first -- whole pages came back
        # as a flat 0.95 or 0.7, which looked exactly like a hardcoded value
        # but was actually real Tesseract output arriving untied. Ordering by
        # (score, confidence) keeps every existing winner and only decides the
        # cases that were previously arbitrary.
        #
        # _hindi_line_score ranks on printed labels rather than Devanagari
        # character count; see its docstring for why the count inverted itself.
        best = max(candidates, key=lambda x: (_hindi_line_score(x[0]), x[1]))
        return best[0], best[1]
    except Exception as exc:
        log.debug("Hindi Tesseract OCR failed: %s", exc)
        return None, None


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
    """Rank competing reads by how many printed field labels survived. Higher is better.

    The obvious ranking -- count every Devanagari character -- is wrong, and
    wrong in the worst possible direction. A read that shatters the glyphs
    yields MORE Devanagari characters than a clean one, because noise stays
    inside the Devanagari block: on 2026-EROLLGEN-S28-40-HIN-107 (Lansdown,
    1983x2806 scan) the thresholded variant scored 618-775 against 122 for the
    clean read, and won every card. Worse, it scored highest precisely
    because it was unreadable, so the worse the read the better the score.
    Tesseract's own confidence reported the truth (19.3 vs 83.7) but was only
    consulted as a tie-break, and scores never tied.

    Printed labels are the honest signal: a real card prints नाम, पिता/पति/
    माता/अन्य, मकान, आयु and लिंग. A read that lost them has lost the row
    that carried the name. Measured over 60 cards per roll:

        roll          deva-count score      label score
        S24-53   150 fields (works)     149 fields
        S28-40     6 fields (broken)   155 fields

    and on S24 the two strategies disagree on 6 cards, where the label score
    is equal or better every time (e.g. किशनप्राल -> किशनपाल).
    """
    text = " ".join(lines)
    return sum(weight for label, weight in HINDI_FIELD_LABELS if label in text)


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
            _tess_count("line_variants")
            text = _tess_string(
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


def _extract_serial_mark(card_img: Any) -> str:
    """Digits in the small box beside the serial, on an addition-list card.

    A normal card prints one serial box. An addition-list card prints a
    second, narrower box to its right (1, 2, …). The box is found from its
    own vertical rules, so a normal card — which has no second box — returns
    nothing instead of a digit imagined from the gap.
    """
    try:
        if card_img is None or getattr(card_img, "size", 0) == 0:
            return ""
        height, width = card_img.shape[:2]
        top = card_img[: max(1, int(height * 0.24))]
        gray = top if top.ndim == 2 else cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
        ink = (gray < 160).astype(np.uint8)
        v_len = max(8, int(top.shape[0] * 0.45))
        vertical = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len)),
        )
        xs = [i for i, v in enumerate(vertical.sum(axis=0)) if v > v_len * 0.6]
        lines: list[list[int]] = []
        for x in xs:
            if not lines or x - lines[-1][-1] > 3:
                lines.append([x])
            else:
                lines[-1].append(x)
        centers = [int(sum(group) / len(group)) for group in lines]
        box = None
        for left, right in zip(centers, centers[1:]):
            start = left / width
            gap = (right - left) / width
            if 0.22 <= start <= 0.45 and 0.12 <= gap <= 0.26:
                box = (left + 2, right - 2)
                break
        if box is None or box[1] <= box[0]:
            # The card does not print the second box. Say so, instead of
            # leaving the cell blank — a blank reads as a missed value.
            return "Not printed"
        crop = top[:, box[0]:box[1]]
        line = _ink_line_of_text(crop, rightmost=False)
        if line is None:
            return ""
        reads = [
            re.sub(r"\D", "", read)
            for read in _read_line_variants(
                line,
                "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789",
                targets=(54, 72, 90),
            )
            if read
        ]
        reads = [read for read in reads if read and len(read) <= 2]
        if not reads:
            return ""
        votes = Counter(reads)
        best, count = max(votes.items(), key=lambda item: (item[1], -len(item[0])))
        # Four of the six reads have to agree. A single imagined digit is
        # how a normal card's empty gap was turning into a 2.
        return best if count >= 4 else ""
    except Exception as exc:
        log.debug("Serial-mark OCR failed: %s", exc)
        return ""


# Two printed ID shapes are valid. The common one is three letters and
# seven digits, e.g. "GKM5933973". Older rolls print a state and three
# number groups, e.g. "UP/79/389/0654035". A read that matches either
# shape is voted on; anything else is only a fallback.
EPIC_SHAPE_RE = re.compile(r"^(?:[A-Z]{3}\d{7}|[A-Z]{2}/\d{2}/\d{3}/\d{7})$")
_COMPACT_SLASHED_EPIC_RE = re.compile(r"^([A-Z]{2})(\d{2})(\d{3})(\d{7})$")

# How close the 8 EPIC readers must be before the record is flagged for human
# review. At 4, a 4-4 tie is flagged and a 5-3 or better majority is not.
#
# Flagging every split flagged the vote working rather than a problem: 15 of
# 180 EPICs in pages 3-8 had a reader disagree, 12 of those on a single glyph
# (5 read as 8, or X as Y). On the seven splits the verified ground truth
# covers, the majority was correct every time and the minority wrong every
# time -- one bad reader out of eight, not a close call. Below this bar the
# record is left unflagged; the dissent still goes to the debug log.
EPIC_SPLIT_REVIEW_TOP_VOTES = 4

# Scratch slot for the last EPIC vote's disagreement, consumed by the caller
# immediately after the read. A bare return value would force both call sites
# to re-run the 8 readers to recover the split. Thread-local, not global:
# _extract_card runs in a ThreadPoolExecutor, so a shared slot would let one
# card's clear() erase another's split mid-read.
_EPIC_VOTE_SPLIT = threading.local()


def _extract_epic_with_tesseract(img_bytes: bytes) -> Optional[list[str]]:
    """Extract EPIC (3 letters + 7 digits) across scales/thresholds.

    Votes on correctly-shaped reads and returns the most common one. A wrong
    digit is still shape-valid, so validation cannot catch it -- the only
    in-band evidence is that the 8 readers disagree with each other. That
    split is recorded on the module-level scratch slot so the caller can flag
    the record for review. The winner is never changed on its account: a
    disagreement means "a human should check this", not "pick a different
    voter's ID", and the alternative candidate may well belong to the reader
    that was wrong rather than to the record.
    """
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
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/",
            targets=(60, 80, 100, 120))
        reads = [_fix_epic_shape(r.upper()) for r in reads if r]
        if not reads:
            return None
        valid = [r for r in reads if EPIC_SHAPE_RE.match(r)]
        pool = valid or reads
        votes = Counter(pool)
        # Ties break on length, then on insertion order.
        best = max(votes, key=lambda r: (votes[r], len(r)))
        top = votes[best]

        # A split is only worth a human's attention when the 8 readers were
        # genuinely undecided. Flagging every split flagged the vote working:
        # 15 of 180 EPICs in pages 3-8 had at least one reader disagree, and 12
        # of those were a single glyph, 5 read as 8 (or X read as Y). On the
        # seven splits ground truth covers, the majority was right every single
        # time and the minority wrong every single time -- the disagreement was
        # one bad reader out of eight, not a close call.
        #
        # So the bar is a genuine contest. Below it, the dissent is recorded
        # for the audit trail but the record is not flagged: 6-1 and 7-1 both
        # mean the mechanism resolved the card, and raising needs_review for
        # them pushed otherwise-clean records into manual review for nothing.
        # Above it, the winner really is a coin-flip between two shapes and the
        # chosen ID may well not be this voter's.
        if len(votes) > 1 and top <= EPIC_SPLIT_REVIEW_TOP_VOTES:
            _EPIC_VOTE_SPLIT.data = [
                {"value": value, "votes": count}
                for value, count in votes.most_common()
            ]
        elif len(votes) > 1:
            log.debug(
                "EPIC readers split %s; majority %d/8 resolved it, not flagged",
                dict(votes), top,
            )
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
            _tess_count("epic_english")
            text = _tess_string(
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


def _slashed_voter_id(text: str) -> str:
    """Return ``UP/79/389/0654035`` when the read is that printed form.

    Slashes are part of the ID. A compact read that kept the two state
    letters and the twelve digits, but lost the slashes, is the same ID.
    A normal three-letter EPIC does not match this and is left alone.
    """
    upper = re.sub(r"\s+", "", (text or "").upper()).replace("\\", "/")
    upper = re.sub(r"/+", "/", upper).strip("/")
    if re.fullmatch(r"[A-Z]{2}/\d{2}/\d{3}/\d{7}", upper):
        return upper
    compact = re.sub(r"[^A-Z0-9]", "", upper)
    match = _COMPACT_SLASHED_EPIC_RE.fullmatch(compact)
    if match:
        return "/".join(match.groups())
    return ""


def _fix_epic_shape(text: str) -> str:
    """Keep a slashed state ID, otherwise force 3 letters + digits.

    The letter form keeps the first 3 characters and forces the rest to
    digits (repairs S->5). Returns unchanged if too short.
    """
    slashed = _slashed_voter_id(text)
    if slashed:
        return slashed
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
            _tess_count("house_raw")
            txt_raw = _tess_string(
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
            _tess_count("house_whitelist")
            text = _tess_string(
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
            _tess_count("house_fallback")
            text = _tess_string(
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


def _extract_house_number_with_tesseract_both(img_bytes: bytes):
    """Return (digit_value, raw_value) from one pass over the crop.

    The raw and normalised readers used to run the no-whitelist pass twice
    over the same pixels (once via raw=True, once via the digit path). Run it
    once and split the result: raw is the first variant's text, digit is the
    normalised extraction from the same reads.
    """
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None, None

        scale = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, img.shape[0])))
        img_scaled = cv2.resize(img, (0, 0), fx=scale, fy=scale,
                                interpolation=cv2.INTER_CUBIC)

        gray_raw = cv2.cvtColor(img_scaled, cv2.COLOR_BGR2GRAY)
        raw_val = None
        digit_val = None
        for _cfg in ("--oem 3 --psm 7", "--oem 3 --psm 6"):
            _tess_count("house_raw")
            txt_raw = _tess_string(
                Image.fromarray(gray_raw), lang=TESSERACT_ENGLISH_LANG,
                config=_cfg,
            ).strip()
            if raw_val is None:
                raw_val = txt_raw or None
            m_frac = re.search(r"(\d{1,5})\s*/\s*(\d{1,4})", txt_raw)
            if m_frac:
                digit_val = f"{m_frac.group(1)}/{m_frac.group(2)}"
                return digit_val, raw_val

        img_resized = _strip_label_colon(img_scaled)
        digits_region = _house_digits_region(img_resized)
        if digits_region:
            x0, y0, x1 = digits_region
            img_resized = img_resized[y0:, x0:x1] if x1 > x0 else img_resized

        img_resized = _tighten_house_crop(img_resized)
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)
        variants = (gray, cv2.threshold(gray, 0, 255,
                                       cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
        for variant in variants:
            _tess_count("house_whitelist")
            text = _tess_string(
                Image.fromarray(variant),
                lang=TESSERACT_ENGLISH_LANG,
                config=TESSERACT_HOUSE_CONFIG,
            )
            runs = re.findall(r"\d+", text)
            if runs:
                digit_val = max(runs, key=len)
                return digit_val, raw_val

        scale2 = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, img.shape[0])))
        img_full = cv2.resize(img, (0, 0), fx=scale2, fy=scale2,
                              interpolation=cv2.INTER_CUBIC)
        gray_full = cv2.cvtColor(img_full, cv2.COLOR_BGR2GRAY)
        for variant in (gray_full,
                        cv2.threshold(gray_full, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            _tess_count("house_fallback")
            text = _tess_string(
                Image.fromarray(variant),
                lang=TESSERACT_ENGLISH_LANG,
                config=TESSERACT_HOUSE_CONFIG,
            )
            runs = re.findall(r"\d+", text)
            if runs:
                digit_val = max(runs, key=len)
                return digit_val, raw_val
        return digit_val, raw_val
    except Exception as exc:
        log.debug("English house-number OCR failed: %s", exc)
        return None, None


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


def _labelled_house_text(lines: Optional[list[str]]) -> str:
    """Return a Hindi-only house value from an explicitly labelled house row.

    The numeric reader deliberately returns ``None`` for values such as
    ``विष्णु``.  That is correct for its job, but it used to mean a clear
    Devanagari house value was discarded when the full-card parser also missed
    that row.  Do not use the generic third-line fallback here: a value is
    eligible only when the same OCR line contains the printed ``मकान संख्या``
    label.  This keeps a nearby name or address continuation from becoming a
    house number.
    """
    candidates: list[str] = []
    for line in lines or []:
        if not _HOUSE_ANY_LABEL_RE.search(line or ""):
            continue
        value = _HOUSE_ANY_LABEL_RE.sub(" ", line)
        # Tesseract sometimes leaves zero-width joiners after a final halant.
        # They are OCR transport noise, not part of the printed value.
        value = re.sub(r"[\u200b-\u200d\ufeff]", "", value)
        value = re.sub(r"\s+", " ", value).strip(" :ः;,.|-—–")
        if not re.search(rf"[{_DEV_LETTER}]", value):
            continue
        # A one-consonant value is normally a plot-letter prefix and needs a
        # numeric companion; keep this path for actual Hindi text values only.
        if _devanagari_consonants(value) < 2:
            continue
        candidates.append(value)
    return max(candidates, key=len, default="")


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


def _place_recovered_leading_one(house: str) -> str:
    """Put a recovered 1 on the first digit, not in front of a letter.

    Paddle reading ``108`` where the Hindi line has ``ए-08`` means the thin
    ``1`` was dropped from the number. Prefixing the whole string stored
    ``1ए-08``, a different house. ``64`` against ``164`` still becomes
    ``164``, because that string already starts on a digit.
    """
    match = re.search(r"\d", house)
    if not match:
        return "1" + house
    return house[:match.start()] + "1" + house[match.start():]


def _strip_house_digit_glue(house: str) -> str:
    """Drop a single junk glyph sitting between two digits.

    ``16व4``, ``102य4`` and ``16&4`` are the number with one misread stroke
    in the middle. A real word such as ``वार्ड`` is left alone because the
    letter is not followed by a digit.
    """
    return re.sub(r"(?<=\d)[वय&](?=\d)", "", house)


# Telugu (U+0C00) and Kannada (U+0C80) use the same letter order. The index
# is the offset inside that block. Hindi has no short ए/ओ, so those map to
# the long vowel a name is actually pronounced with.
_BRAHMIC_INDEX_TO_DEVA = {
    0x01: "\u0901", 0x02: "\u0902", 0x03: "\u0903",
    0x05: "अ", 0x06: "आ", 0x07: "इ", 0x08: "ई", 0x09: "उ", 0x0A: "ऊ",
    0x0B: "ऋ", 0x0E: "ए", 0x0F: "ए", 0x10: "ऐ", 0x12: "ओ", 0x13: "ओ",
    0x14: "औ",
    0x15: "क", 0x16: "ख", 0x17: "ग", 0x18: "घ", 0x19: "ङ",
    0x1A: "च", 0x1B: "छ", 0x1C: "ज", 0x1D: "झ", 0x1E: "ञ",
    0x1F: "ट", 0x20: "ठ", 0x21: "ड", 0x22: "ढ", 0x23: "ण",
    0x24: "त", 0x25: "थ", 0x26: "द", 0x27: "ध", 0x28: "न",
    0x2A: "प", 0x2B: "फ", 0x2C: "ब", 0x2D: "भ", 0x2E: "म",
    0x2F: "य", 0x30: "र", 0x31: "र", 0x32: "ल", 0x33: "ळ",
    0x35: "व", 0x36: "श", 0x37: "ष", 0x38: "स", 0x39: "ह",
    0x3E: "ा", 0x3F: "ि", 0x40: "ी", 0x41: "ु", 0x42: "ू",
    0x43: "ृ", 0x44: "ॄ", 0x46: "े", 0x47: "े", 0x48: "ै",
    0x4A: "ो", 0x4B: "ो", 0x4C: "ौ", 0x4D: "्",
}
_FOREIGN_NAME_SCRIPTS = (
    (0x0B00, 0x0B7F),  # Odia
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
)
_FOREIGN_RELATION_LANGS = ("tel", "kan", "ori")
_FOREIGN_RELATION_FIELD = {
    "पिता": "voter_father_name",
    "पति": "voter_husband_name",
    "माता": "voter_mother_name",
    "अन्य": "voter_other_name",
}


def _brahmic_to_devanagari(text: str) -> str:
    """Turn Telugu or Kannada letters into the same Hindi word.

    The original spelling is kept sound for sound. A virama at the end of a
    word is how those scripts write a final consonant; Hindi writes that
    consonant with its inherent vowel, so భూషణ్ becomes भूषण. A virama
    inside a word stays, so చంద్ర stays चंद्र.
    """
    words = []
    for raw in str(text or "").split():
        pieces = []
        for ch in raw:
            if ch in "\u200c\u200d":
                continue
            cp = ord(ch)
            mapped = None
            for base, end in _FOREIGN_NAME_SCRIPTS:
                if base <= cp <= end:
                    mapped = _BRAHMIC_INDEX_TO_DEVA.get(cp - base, "")
                    break
            if mapped is None and ("\u0900" <= ch <= "\u097F" or ch.isdigit()):
                mapped = ch
            if mapped:
                pieces.append(mapped)
        word = "".join(pieces)
        if word.endswith("\u094d"):
            word = word[:-1]
        if re.search(r"[अ-हक़-य़]", word):
            words.append(word)
    return " ".join(words)


def _foreign_name_as_devanagari(line: str) -> str:
    """Take the non-Hindi name after the label and return it in Hindi."""
    parts = re.split(r"[:：;ः!]", str(line or ""), maxsplit=1)
    body = parts[1] if len(parts) > 1 else parts[0]
    runs = re.findall(r"[\u0C00-\u0C7F\u0C80-\u0CFF]+", body)
    if not runs:
        return ""
    hindi = _brahmic_to_devanagari(" ".join(runs))
    if len(re.findall(r"[अ-हक़-य़]", hindi)) < 4:
        return ""
    return hindi


def _agreed_foreign_name(reads: list[str]) -> str:
    """Accept a converted name only when two reads produce the same Hindi."""
    names = [name for name in (_foreign_name_as_devanagari(read) for read in reads) if name]
    if len(names) >= 2 and len(set(names)) == 1:
        return names[0]
    return ""


def _relation_kind_in_text(text: str) -> str:
    """The Hindi label on a mixed-script line. The name itself may not be Hindi."""
    if "माता" in text:
        return "माता"
    if "पिता" in text:
        return "पिता"
    if "पति" in text:
        return "पति"
    if "अन्य" in text:
        return "अन्य"
    return ""


def _read_foreign_relation(card_img: Any) -> tuple[str, str]:
    """Read a Telugu or Kannada relative name and return it as Hindi.

    Runs only as a fallback, and only when that language is installed.
    Two scales have to convert to the same Hindi word. A dot or an empty
    label converts to nothing.
    """
    try:
        installed = set(pytesseract.get_languages())
    except Exception as exc:
        log.debug("Foreign-script language list failed: %s", exc)
        return "", ""
    langs = [lang for lang in _FOREIGN_RELATION_LANGS if lang in installed]
    if not langs:
        return "", ""
    crop = _crop_card_region(card_img, RELATION_REGION, "FOREIGN_RELATION")
    if crop is None or getattr(crop, "size", 0) == 0:
        return "", ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if int((gray < 180).sum()) < 40:
        return "", ""
    for lang in langs:
        reads = []
        kind = ""
        for scale in (3, 4):
            big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            try:
                text = pytesseract.image_to_string(
                    big, config=f"--oem 3 --psm 7 -l hin+{lang}",
                )
            except Exception as exc:
                log.debug("Foreign relation read failed: %s", exc)
                continue
            text = " ".join(text.split())
            if not text:
                continue
            reads.append(text)
            kind = kind or _relation_kind_in_text(text)
        name = _agreed_foreign_name(reads)
        if name and kind:
            return kind, name
    return "", ""


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



# Reader set for the deleted-mark vote. A single Paddle read of the serial
# box is not reliable: on page 16 card 20 (serial 410, AWX4756151) the same
# crop read as ['410'] in the stacked pass and ['Q', '410'] in isolation, so
# one read can lose a marker that is plainly legible. Scaling to fixed pixel
# heights and thresholding gives several genuinely different views of the
# same glyph instead of several copies of one view.
#
# 200 DPI on purpose. The marker is a thin enclosing loop; at 300 DPI two of
# the five known-deleted cards dropped to 3/8 and would have been lost by any
# majority bar. This matches the rest of the pipeline, which reads Hindi at
# 200 for the same reason.
DELETED_VOTE_HEIGHTS_PX = (40, 60, 80, 120)
# Thresholding AFTER the upscale, not before. Order matters more than it
# looks: Otsu-then-upscale yields a blurred, grey-edged binary (the upscale
# interpolates across the threshold), and a blurred threshold is exactly what
# erases the thin loop of a Q. Upscale-then-Otsu thresholds at the resolution
# the recogniser actually sees. Measured on the same crop, same 8 readers,
# the two orders differ: page 16 cards 11 and 20 scored 4/8 before and 8/8
# after, which is the whole reason those two cards were being missed.
#
# 8 readers = 4 pixel heights x {plain, otsu}. Bar set from the full 600-card
# roll rather than from the cases that motivated it: every known-deleted card
# scores 8/8 and the other 595 cards score 0/8, so any bar in 1..8 is safe.
# 5 was chosen from six samples and rejected two real deletions for nothing.
DELETED_VOTE_MIN_HITS = 5

# The rule needs no OCR intelligence: THE SERIAL BOX CONTAINS ONLY DIGITS, so
# any alphabetic character in it is a deletion marker. Nothing else can
# legitimately appear there, which makes this a strict rule rather than a
# heuristic.
#
# It is deliberately not written as "Q". Q is only how one reader renders the
# marker; the same card reads as 'Q', '[a', 'or' or nothing at all depending on
# the render, and other rolls strike with a different letter altogether. Pinning
# the test to Q therefore matched one reader's spelling of the mark rather than
# the mark itself, and every card whose letter came back as anything else was
# written as is_deleted = false. The rule that was always right is the one the
# Tesseract crosscheck already used: is there a letter here?
#
# Because this is per-reader over the OCR's own token list, a stray letter from
# a mangled digit ("285" read as "Z85") is the accepted cost, and the majority
# bar is what keeps it from deciding anything on its own.
DELETED_MARKER_RE = re.compile(r"[A-Za-z]")


def _serial_deleted_vote(serial_img: Any) -> tuple[bool, int, int]:
    """Decide is_deleted by voting across several reads of the serial box.

    Returns (is_deleted, hits, total_readers). Every reader is a different
    rendering of the same crop -- fixed pixel height, then plain and Otsu --
    and the mark counts as present only if a majority of them see it.

    Voting rather than trusting one read is the fix for a marker that is
    legible but inconsistently detected; it makes the decision robust to a
    single bad read without needing that read to be impossible.

    Note the limit of this method: every reader is the SAME engine, so the
    vote buys tolerance to a bad render, not to a bad model. When Paddle is
    systematically blind to a given glyph the vote cannot rescue it -- that
    is what _serial_deleted_tesseract_crosscheck is for.

    `total` is the number of readers ACTUALLY RUN, not the size of the reader
    set. The early exit means a decided card reports fewer than eight. The
    verdict itself is unchanged -- both exit bounds are monotonic over a fixed
    reader set -- but callers must not read `total` as the set size.
    """
    if serial_img is None or getattr(serial_img, "size", 0) == 0:
        return False, 0, 0
    height = serial_img.shape[0]
    if not height:
        return False, 0, 0
    hits = 0
    total = 0
    # The reader set is fixed -- four pixel heights x {plain, Otsu} -- and the
    # verdict is `hits >= DELETED_VOTE_MIN_HITS` over that set. Both bounds
    # below are therefore monotonic: no reader left unrun can change the
    # answer, so stopping early is exact, not approximate.
    #
    #   hits >= MIN_HITS                     -> deleted, already decided
    #   hits + readers_left < MIN_HITS       -> not deleted, already decided
    #
    # The saving is large because the miss case is a clean sweep: on 210 real
    # crops the hit distribution was {0 hits: 206, 7: 1, 8: 3}, so 206 of 210
    # cards lock on the second bound after 4 of 8 readers. This is the single
    # most expensive operation in the pipeline -- ~270 ms/card, 8 of its 9
    # Paddle calls -- so halving it is worth the two extra comparisons.
    readers_left = len(DELETED_VOTE_HEIGHTS_PX) * 2
    for target in DELETED_VOTE_HEIGHTS_PX:
        scale = target / float(height)
        if scale <= 0:
            readers_left -= 2
            continue
        upscaled = cv2.resize(
            serial_img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        variants = [upscaled]
        gray = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY) if upscaled.ndim == 3 else upscaled
        otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        variants.append(
            cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR)
            if otsu.ndim == 2
            else otsu
        )
        for variant in variants:
            total += 1
            readers_left -= 1
            try:
                result = _get_paddle().ocr(variant, cls=False)
            except Exception as exc:
                log.debug("deleted vote reader failed: %s", exc)
                continue
            texts = [ln[1][0].strip() for ln in (result[0] or []) if ln[1][0]]
            if any(DELETED_MARKER_RE.search(t) for t in texts):
                hits += 1
            if hits >= DELETED_VOTE_MIN_HITS:
                return True, hits, total
            if hits + readers_left < DELETED_VOTE_MIN_HITS:
                return False, hits, total
    if not total:
        return False, 0, 0
    return hits >= DELETED_VOTE_MIN_HITS, hits, total


# The second engine for the deleted mark. Paddle and Tesseract fail on
# different cards -- on the ground truth Paddle reads the mark on all five
# while Tesseract reads nothing at all on page 15 card 30 -- so neither alone
# is sufficient and the two are combined with OR.
#
# Same rule as the Paddle vote, for the same reason (see DELETED_MARKER_RE):
# the serial box holds a number, so a letter in it is a deletion marker. On the
# ground truth it reads the literal string 'Q 410' on page 16 card 20 and
# '[a 422]' / '[aor]' on two others -- three spellings of one mark.
DELETED_TESSERACT_MIN_HITS = 4


def _serial_deleted_tesseract_crosscheck(serial_img: Any) -> tuple[int, int]:
    """Look for the deleted marker with a different engine than Paddle.

    Returns (hits, total_readers) counting readers whose text contains any
    letter. The serial box holds a number, so a letter in it is a deletion
    marker.

    This exists because the Paddle vote is eight renders of ONE recogniser: it
    buys tolerance to a bad render, not to a bad model. When Paddle is
    systematically blind to a glyph, no amount of re-rendering recovers it, and
    a second engine is the only thing that can.
    """
    if serial_img is None or getattr(serial_img, "size", 0) == 0:
        return 0, 0
    height = serial_img.shape[0]
    if not height:
        return 0, 0
    hits = 0
    total = 0
    for target in DELETED_VOTE_HEIGHTS_PX:
        scale = target / float(height)
        if scale <= 0:
            continue
        upscaled = cv2.resize(
            serial_img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        gray = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY) if upscaled.ndim == 3 else upscaled
        variants = [gray]
        otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        variants.append(otsu)
        for variant in variants:
            total += 1
            try:
                _tess_count("deleted_crosscheck")
                text = _tess_string(
                    Image.fromarray(variant), lang="eng",
                    config="--oem 3 --psm 7",
                )
            except Exception as exc:
                log.debug("deleted tesseract crosscheck reader failed: %s", exc)
                continue
            if DELETED_MARKER_RE.search(text):
                hits += 1
    return hits, total


def _decide_deleted(
    serial_img: Any, *, force_crosscheck: bool = False,
) -> tuple[bool, int, int]:
    """Decide is_deleted, escalating to a second engine only when unsure.

    Returns (is_deleted, hits, total_readers) from whichever arm decided.

    The cheap path is the Paddle vote, which runs on every record. Tesseract
    is a subprocess per reader, so it is consulted ONLY when the Paddle vote is
    genuinely inconclusive -- a clear majority, or a clear zero, is not worth a
    second opinion, and no card in the 600-card roll landed in the band. The
    crosscheck is a safety net for marks this roll happens not to contain, not
    a routine cost on every card.
    """
    marked, hits, total = _serial_deleted_vote(serial_img)
    if not total:
        return False, 0, 0
    if marked:
        return marked, hits, total
    if (not force_crosscheck
            and hits + (len(DELETED_VOTE_HEIGHTS_PX) * 2 - total)
            < DELETED_VOTE_MIN_HITS):
        # Decided outright, either way: the vote reached the threshold, or ran
        # out of readers still unable to. The second reading of the miss bound
        # is what keeps the early exit from widening the escalation band -- a
        # card that locked at 2-of-4 is NOT inconclusive, it is decided.
        return False, hits, total

    # Inconclusive readings normally use the second engine.  A caller can
    # also force it for a card whose gender never came back: a deleted
    # watermark can make Paddle return zero votes while Tesseract still sees
    # the marker.  A missing age is not that case — age is read after this
    # decision — and a forced crosscheck there marked live voters deleted.
    t_hits, t_total = _serial_deleted_tesseract_crosscheck(serial_img)
    if t_total and t_hits >= DELETED_TESSERACT_MIN_HITS:
        return True, hits, total
    return marked, hits, total


def _detect_deleted_watermark(card_img: Any, serial_png: Optional[bytes] = None) -> bool:
    """Detect the deletion marker in the card's serial box.

    The serial box holds a number and nothing else, so any letter in it is the
    deletion mark (see DELETED_MARKER_RE). Decided by the same reader vote the
    pipeline writes, so a caller gating on this gets the answer the extractor
    would have written rather than a second, weaker opinion.

    `serial_png` is the pre-cropped box when the caller has one. When it does
    not, the box is cropped from `card_img` using the same region the extractor
    uses. That fallback matters: the only caller outside this module passed a
    card and no crop, so it took the old early return and every card came back
    un-deleted. Returning False there was not an answer, it was a silence.
    """
    try:
        serial_img = None
        if serial_png is not None:
            serial_img = cv2.imdecode(np.frombuffer(serial_png, np.uint8),
                                     cv2.IMREAD_COLOR)
        if (serial_img is None or serial_img.size == 0) and card_img is not None:
            serial_img = _crop_card_region(
                card_img, TESSERACT_CARD_CONTENT_REGIONS[0], "SERIAL")
        if serial_img is None or serial_img.size == 0:
            return False

        marked, _hits, total = _decide_deleted(serial_img)
        return marked if total else False
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
    """Read EPIC from card image bytes for voter grid probe. Returns empty string on failure.

    A read only counts as a read if it is actually EPIC-shaped. `_voter_card_rects()`
    hands out thirty rectangles on every page of every roll -- the cover, the
    header band and the trailing summary page included -- so on a page with no
    cards the probe still crops thirty regions of whatever text happens to be
    there. Left unchecked, OCR scrapes shaped like "77T55" or "V" out of that
    text and the page is admitted as a voter grid, producing thirty phantom
    records with no name and no age. Shape is the cheapest test that separates
    "an ID number is printed here" from "some ink is printed here".
    """
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
        if not reads or not reads[0]:
            return ""
        return reads[0] if EPIC_SHAPE_RE.match(reads[0]) else ""
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

    def gender_from_text(value: str) -> str:
        """Return a canonical gender only from an explicit printed value.

        The rolls use ``पुरुष``/``महिला``, the older ``पुरूष``/``स्त्री``
        spellings, and ``तृतीय लिंग``.  Treating only the first pair as valid
        silently loses otherwise legible cards -- and because the third value
        matches neither branch, a perfectly readable ``लिंग : तृतीय लिंग``
        falls through to empty and lands in the blank pile.  This is
        deliberately a closed vocabulary, used only on the age/gender row, so
        it cannot turn a person's name into a gender.

        Third gender is tested first: its ``तृ``/``त्र`` anchor cannot occur
        inside either of the other two words, so the ordering costs nothing
        and keeps the longest value from being clipped by a shorter match.
        """
        compact = re.sub(r"[\s:;ः,.|]", "", value or "")
        if (THIRD_GENDER_RE.search(compact)
                or re.search(r"(?:^|[^a-z])third(?:$|[^a-z])",
                             compact.lower()) is not None):
            return "तृतीय लिंग"
        if (re.search(r"प[ुू]र[ुू]?[षश]", compact)
                or re.search(r"म[रड][्]?द", compact)
                or re.search(r"(?:^|[^a-z])(?:male|mard)(?:$|[^a-z])",
                             compact.lower()) is not None
                or re.search(r"(?:^|[^ऀ-ॿ])नर(?:$|[^ऀ-ॿ])", compact)):
            return "पुरुष"
        if (re.search(r"म[ऀ-ॿ]*ल[ऀ-ॿ]*ा", compact)
                or re.search(r"स्त्री|स्त्रि|स्तरी", compact)
                or "औरत" in compact or "नारी" in compact
                or any(token in compact.lower()
                       for token in ("mahila", "female", "aurat", "naari"))):
            return "महिला"
        return ""

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
    # Low-contrast cards can read the initial stroke of ``नाम`` as ``भाम``.
    # Accept it only in the label pattern, never as a free-text replacement.
    _नाम_LBL = r"(?:नाम|नाग|भाम|nama)"
    # The roll prints four relation words. A misread is still one of those
    # four: the consonants stay प+त, म+त, or अ+न+य, and the vowel says
    # whether प+त is पिता or पति. का and नाम sit between that word and the
    # separator, so their spelling never has to be listed. The value is the
    # text after the separator.
    _REL_FIELD = {
        "पिता": "voter_father_name",
        "पति": "voter_husband_name",
        "माता": "voter_mother_name",
        "अन्य": "voter_other_name",
    }
    _REL_SEP = ":：;ः!"

    def _word_char(ch: str) -> bool:
        if ch in _REL_SEP or ch.isspace():
            return False
        o = ord(ch)
        return 0x0900 <= o <= 0x097F or ch.isalpha()

    def _skeleton(token: str) -> str:
        """Consonants only. Matras and the virama are how the misreads differ."""
        return "".join(
            ch for ch in token
            if ch != "्" and unicodedata.category(ch) not in ("Mn", "Mc")
        )

    def _classify_head(head: str) -> str:
        """Map one token, or a prefix of one, onto a printed relation word."""
        if head == "क्वा":
            # The whole label collapsed into this one conjunct.
            return "पति"
        low = head.lower()
        if low in ("anya", "anye"):
            return "अन्य"
        sk = _skeleton(head)
        if sk in ("अन्य", "अनय"):
            return "अन्य"
        if sk.startswith("म") and "त" in sk and set(sk) <= set("मतर"):
            return "माता"
        if sk.startswith("प") and "त" in sk and set(sk) <= set("पतर"):
            # ा, or a vowel written on the first consonant (ि before त in
            # Unicode order), is पिता. ि after त is पति. पति and पिता share
            # the consonant skeleton, so the vowel is the whole distinction.
            tpos = head.find("त")
            if "ा" in head or any(v in head[:tpos] for v in "िीुूेैोौ"):
                return "पिता"
            return "पति"
        return ""

    def _label_separator(text: str, i: int) -> int:
        """Index of the separator that ends a relation label, or -1.

        का / नाम and any misread of them are optional and are not matched
        letter by letter. A short token that is only क, or क glued to नाम,
        is the label tail. Anything else is the relative's name, and this
        is not a label.
        """
        n = len(text)
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] in _REL_SEP:
            return i

        def token_at(pos: int) -> tuple[str, int]:
            j = pos
            while j < n and _word_char(text[j]):
                j += 1
            return text[pos:j], j

        token, j = token_at(i)
        if not token:
            return -1
        sk = _skeleton(token)
        # काम is का with an extra म. It is still the label, not the name.
        ka = bool(sk) and set(sk) <= set("कषरम") and len(token) <= 4
        # नाम is the most damaged glyph on a faint card. These skeletons are
        # the reads actually seen where the value after the colon was a real
        # relative: नात्र, नाग्र, नागर, नाम्र, नांग्र, साम, चाम, साग.
        # They count only in this label slot, and only when a separator follows.
        naam = sk in (
            "नम", "नथ", "नग", "भम",
            "नतर", "नगर", "नमर",
            "सम", "चम", "सग",
        )
        glued = sk.startswith("क") and "न" in sk and any(c in sk for c in "मथग")
        if not (ka or naam or glued):
            return -1
        i = j
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] in _REL_SEP:
            return i
        if ka:
            token2, j2 = token_at(i)
            if _skeleton(token2) in (
                "नम", "नथ", "नग", "भम",
                "नतर", "नगर", "नमर",
                "सम", "चम", "सग",
            ):
                i = j2
                while i < n and text[i].isspace():
                    i += 1
                if i < n and text[i] in _REL_SEP:
                    return i
        return -1

    def _match_relation_at(text: str, start: int) -> tuple[str, int]:
        """A relation label starting at start. Returns (kind, end) or ('', -1).

        end is just past the separator, which is what the line splitter
        needs so the value stays on the relation piece.
        """
        if start > 0 and _word_char(text[start - 1]):
            return "", -1
        end = start
        while end < len(text) and _word_char(text[end]):
            end += 1
        word = text[start:end]
        # Longest prefix first. 'पतिका' is पति + का, not पत + िका.
        for cut in range(len(word), 1, -1):
            kind = _classify_head(word[:cut])
            if not kind:
                continue
            sep = _label_separator(text, start + cut)
            if sep >= 0:
                return kind, sep + 1
        # One junk consonant glued on the front: 'जअन्यः महताब'.
        if len(word) > 2:
            kind = _classify_head(word[1:])
            if kind:
                sep = _label_separator(text, start + len(word))
                if sep >= 0:
                    return kind, sep + 1
        return "", -1

    def _find_relation(text: str) -> tuple[int, int]:
        """(start, end) of the first relation label in text, or (-1, -1)."""
        for i in range(len(text)):
            if i and _word_char(text[i - 1]):
                continue
            kind, end = _match_relation_at(text, i)
            if kind:
                return i, end
        return -1, -1

    # --- Merged name+relation lines -------------------------------------
    #
    # The roll prints the voter's name and the relation on separate rows, but
    # Tesseract often returns them as ONE line:
    #
    #   'नाम : सचिन मेता पिता का नाम: राजकुमार'
    #
    # The voter-name branch below deliberately declines any line carrying a
    # relation label, and after_colon splits at the FIRST colon -- so the
    # whole thing used to land in voter_father_name with the voter's own name
    # glued to the front of it, and voter_first_name stayed empty. On the
    # live roll that was 479 of 531 records.
    #
    # Both values are already present in the text, so this expands the line
    # into synthetic lines and lets every existing branch below run unchanged.

    # The bare voter-name label, ANCHORED to the start of the line. An
    # unanchored search is wrong here: the 'नाम' inside 'पिता का नाम' is
    # itself a नाम label, so an unanchored test matches nearly every pure
    # relation line (524 lines in the roll's own text layer, vs 18 anchored)
    # and would push the 'फोटो उपलब्ध है' watermark into voter_first_name.
    #
    # The separator after नाम is optional. Tesseract often returns the row
    # as 'नाम चंद्र दीप' with no colon at all. after_colon() then yields
    # nothing, and the voter name is stored blank while the father, house,
    # age and gender on the same card are kept. A following space is enough
    # to separate the label from the name; a glued token ('नामकुमार') is
    # still refused so the label cannot eat the first letter of a name.
    _BARE_NAME_LBL = re.compile(rf"^\s*{_नाम_LBL}(?:\s*[:：;ः!]|\s+)")

    # 'फोटो उपलब्ध है' ("photo available") is stamped over the photo box and
    # bleeds into neighbouring text rows. It is never part of a name.
    _PHOTO_JUNK = re.compile(r"फोटो\s*(?:उपलब्ध\s*(?:है।?|हे?)?)?")
    # Field labels that can share a line with a relation row. Cutting the
    # remainder into its own line is what lets the house/age/gender branches
    # below see them -- today the relation branch claims the line and
    # continue()s, so a house number sharing the row is lost (13 rows of
    # this roll print it that way).
    # आयुष is a relative's name on these rolls. A bare 'आयु' match cuts that
    # name off the relation line and stores the relative as missing. The age
    # label is the word आयु itself, not आयु followed by more Devanagari.
    _FIELD_TAIL = re.compile(
        r"(?:फोटो|मकान\s*संख्या|(?:आयु|आवु|आय|आग|आगु|लिंग)(?![\u0900-\u097F]))"
    )

    def _expand_line(line: str) -> list[str]:
        """Split a merged line into the pieces the branches below expect."""
        stripped = _PHOTO_JUNK.sub(" ", line)
        # Strip trailing separators left behind by the watermark removal, so
        # they are not mistaken for a real colon further down.
        stripped = stripped.strip().strip(":").strip()

        lead = _BARE_NAME_LBL.match(stripped)
        rel_start, rel_end = _find_relation(stripped)
        if rel_start < 0 or (lead and rel_start < lead.end()):
            return [stripped]

        out = []
        if lead:
            left = stripped[lead.end():rel_start]
            # A second relation label on one line ('पिता कानाम इंद्र पाल पिता
            # का नाम:') would otherwise write a label plus a colon into the
            # voter's name, which is worse than leaving it blank.
            if left.strip() and _find_relation(left)[0] < 0 and ":" not in left:
                # A literal label, not _नाम_LBL -- that is a pattern string,
                # and interpolating it would emit "(?:नाम|नाग|nama): ..." as
                # the text, whose first colon sits inside the pattern.
                out.append(f"नाम: {left.strip()}")

        # Everything from the relation label onward. Cut at the first
        # following field label so the house/age/gender branches get a line.
        tail = _FIELD_TAIL.search(stripped, rel_end)
        if tail:
            out.append(stripped[rel_start:tail.start()])
            out.append(stripped[tail.start():])
        else:
            out.append(stripped[rel_start:])
        return out

    expanded: list[str] = []
    for raw in lines:
        expanded.extend(_expand_line(raw))

    # A wrapped house address is the line after मकान संख्या, with no label
    # of its own: 'इलाइचीपुर गजियाबाद'. Age, a name, or a relation ends it.
    _HOUSE_STOP = re.compile(r"आयु|आवु|आय|आग|आगु|लिंग")

    def _house_continuation(text: str) -> str:
        """The wrapped locality, or '' when this line is the next field.

        The Hindi model reads an English locality ('Ghaziabad') as a digit
        soup ('(9॥828/080'). Keep the Devanagari words in front of that and
        drop the rest, so the junk is not written onto the address.
        """
        if _HOUSE_STOP.search(text) or _BARE_NAME_LBL.match(text):
            return ""
        rel_at = _find_relation(text)[0]
        if rel_at >= 0 and not text[:rel_at].strip():
            return ""
        # Latin, a bracket, or a run of digits is where the line left the
        # locality and ran into the photo or an English word.
        head = re.split(r"[A-Za-z(\[{]|[0-9०-९]{2,}", text, maxsplit=1)[0]
        head = head.strip(" ।॥:：,，-–—")
        # The first word is the rest of the address. A later word is kept
        # only when it is itself a place (पुर, बाद, नगर…); otherwise it is
        # the photo edge, as in 'इलाइचीपुर प्ध्टांत'.
        kept: list[str] = []
        for token in head.split():
            if not re.search(r"[अ-हक़-य़]", token):
                break
            if kept and not re.search(r"(पुर|बाद|नगर|विहार|नी|पार्क|सिटी)$", token):
                break
            kept.append(token)
        head = " ".join(kept)
        letters = re.findall(r"[अ-हक़-य़]", head)
        return head if len(letters) >= 2 else ""

    index = 0
    while index < len(expanded):
        line = expanded[index].strip()
        index += 1
        if not line:
            continue

        # Voter name — must NOT match a relation label on the same line.
        # A line that starts with नाम and has no colon is still the voter's
        # name; requiring the colon is what left 'नाम चंद्र दीप' blank.
        starts_with_name = _BARE_NAME_LBL.match(line) is not None
        has_name_label = starts_with_name or re.search(
            rf"{_नाम_LBL}\s*[:：;ः!]", line
        )
        # A relation label anywhere on the line means this is not the voter.
        # 'पिता करा नाम: कामता प्रसाद' still contains नाम:, and taking the
        # text after that colon is what wrote the father over the voter.
        if has_name_label and _find_relation(line)[0] < 0:
            name = after_colon(line)
            if not name and starts_with_name and not re.search(r"[:：;ः!]", line):
                name = clean_value(re.sub(rf"^\s*{_नाम_LBL}\s*", "", line))
            parts = name.split()
            if parts:
                record["voter_first_name"] = parts[0]
                if len(parts) == 2:
                    record["voter_sur_name"] = parts[1]
                elif len(parts) > 2:
                    record["voter_middle_name"] = " ".join(parts[1:-1])
                    record["voter_sur_name"] = parts[-1]
            continue

        # Relation row. The kind comes from the word itself; the name is
        # whatever follows the separator, so का/नाम do not have to match.
        rel_at, _rel_end = _find_relation(line)
        if rel_at == 0 or (rel_at > 0 and not line[:rel_at].strip()):
            kind, _end = _match_relation_at(line, rel_at)
            name = after_colon(line)
            if kind and name:
                record["relation_name"] = kind
                record[_REL_FIELD[kind]] = name
            if kind:
                continue

        # House number. The following unlabelled line is the rest of the
        # address when it wraps, not a new field.
        if re.search(r"मकान\s*संख्या", line):
            parts = [after_colon(line)]
            while index < len(expanded):
                extra = _house_continuation(expanded[index].strip())
                if not extra:
                    break
                parts.append(clean_value(extra))
                index += 1
            house = " ".join(part for part in parts if part)
            record["house_no"] = house.strip(" |'`")
            continue

        # Age and gender can appear on the same line: आयु : 58 लिंग : पुरुष
        if re.search(r"आयु|आवु|आय|आग|आगु", line):
            # Match ASCII or Devanagari digits (e.g. ३6, ३६)
            m = re.search(r"(?:आयु|आवु|आय|आग|आगु)\s*[:：]?\s*([\d०-९]{1,3})", line)
            if m:
                age_val = m.group(1).translate(str.maketrans("०१२३४५६७८९", "0123456789"))
                # A third digit here is the next glyph glued on: 36 stored
                # as 336, 46 as 465. Below 18 is a dropped leading 1.
                # Either one stays unread so the exact-card pass can recover
                # a real age instead of keeping the broken number.
                if AGE_MIN <= int(age_val) <= AGE_MAX:
                    record["age"] = age_val
        # OCR occasionally reads the label as लिग/लि ग, but the value itself
        # remains a closed vocabulary.  Restrict this to the row that also
        # carries age; a name or relation row must never be classified as
        # gender merely because it contains a similar glyph sequence.
        if re.search(r"आयु|आवु|आय|आग|आगु", line) or "लिंग" in line or re.search(r"लि\s*[गं]", line):
            gender = gender_from_text(line)
            if gender:
                record["gender"] = gender

    if any(record.get(k) for k in (
        "voter_first_name", "voter_father_name", "voter_husband_name",
        "voter_mother_name", "voter_other_name", "age", "gender",
    )):
        return record
    return {"empty": True}


def _extract_age_from_clean_card(card_img: Any) -> str:
    """Read only the age value from an already cleaned card image.

    The Hindi model collapses the Devanagari digits these rolls print
    (``४४`` for 44) into a single glyph, so a real two-digit age arrives at
    the parser as ``4`` and is then dropped by the ``>= 18`` floor.  The
    English digit model reads the same crop correctly, so this recovery
    reader locates the value between the ``आयु`` and ``लिंग`` labels and
    reads *that span only* with a digits-only English pass.

    Three attempts in order:
      1. Span crop (between label anchors) → English digits-only
      2. Full band crop → English digits-only
      3. Raw Hindi OCR on full band → label-anchored regex

    Like the gender reader this returns ``""`` rather than a guess.
    """
    try:
        band = _crop_card_region(card_img, GENDER_AGE_REGION, "GENDER_AGE")
        if band is None or band.size == 0:
            return ""
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY) if band.ndim == 3 else band
        # Attempts 1 and 2: span then full band with digit-whitelist configs.
        for attempt, crop in enumerate((_age_value_span(gray), band)):
            if crop is None or getattr(crop, "size", 0) == 0:
                continue
            # PSM 6 recovers thin trailing digits only in the label-bounded
            # age span; using it on the whole band could select an address.
            value = _read_age_digits(crop, allow_block_mode=(attempt == 0))
            if value:
                log.debug("Focused age read (attempt %d): %s", attempt + 1, value)
                return value

        # The label span can be too tight for PSM 6 while a continuation from
        # the preceding address makes full-band PSM 6 unsafe.  The lower half
        # contains only the printed age/gender row on this template.
        lower_band_value = _read_age_from_lower_band(band)
        if lower_band_value:
            log.debug("Focused age read (lower-band recovery): %s", lower_band_value)
            return lower_band_value

        # Attempt 3: raw Hindi OCR on upscaled band with label-anchored regex.
        # Catches cards where the address wraps into the band and the span
        # locator loses the label (e.g. "आग : 41 लिंग : महिला" where "आग"
        # is OCR junk for "आयु").  The pattern matches any age-label variant
        # followed by optional punctuation/spaces, then 1–3 digits.
        up = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        up = cv2.copyMakeBorder(up, 10, 10, 10, 10, cv2.BORDER_REPLICATE)
        for cfg in ("--oem 3 --psm 6 -l hin", "--oem 3 --psm 7 -l hin"):
            try:
                text = pytesseract.image_to_string(up, config=cfg)
            except Exception:
                continue
            # Label variants: आयु आवु आय आग and common OCR mangles, then
            # optional colon/space, then the digits.
            m = re.search(
                r"(?:आयु|आवु|आय|आग|आगु)[^०-९0-9]{0,6}([0-9]{1,3})", text
            )
            if m:
                val = m.group(1)
                # Single digit 18/19 not possible; skip it
                if len(val) == 1:
                    # The label is present but a thin trailing digit was
                    # lost. Read only the lower age row and require its first
                    # digit to agree with the Hindi label-anchored read.
                    lower = gray[gray.shape[0] // 2:, :]
                    for candidate in _read_age_block_digits(lower):
                        if candidate.startswith(val) and AGE_MIN <= int(candidate) <= AGE_MAX:
                            log.debug("Focused age read (Hindi/English recovery): %s", candidate)
                            return candidate
                    continue
                if AGE_MIN <= int(val) <= AGE_MAX:
                    log.debug("Focused age read (attempt 3, label-regex): %s", val)
                    return val
            # Right-anchor fallback: digits sitting just before the gender label.
            # Catches cards where the age label is garbled beyond recognition but
            # the value itself survives right of "आयु : XX लिंग".
            m2 = re.search(
                r"([0-9]{2,3})[^0-9]{0,6}(?:लिंग|लग|लिग|लिं)", text
            )
            if m2:
                val = m2.group(1)
                if AGE_MIN <= int(val) <= AGE_MAX:
                    log.debug("Focused age read (attempt 3, right-anchor): %s", val)
                    return val
        return ""
    except Exception as exc:
        log.debug("Focused age extraction failed: %s", exc)
        return ""


def _read_age_block_digits(crop: Any) -> list[str]:
    """Return valid digit runs from the already isolated lower age row."""
    if crop is None or getattr(crop, "size", 0) == 0:
        return []
    enlarged = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    enlarged = cv2.copyMakeBorder(enlarged, 20, 20, 20, 20, cv2.BORDER_REPLICATE)
    try:
        text = pytesseract.image_to_string(
            enlarged,
            config="--oem 3 --psm 6 -l eng -c tessedit_char_whitelist=0123456789",
        )
    except Exception as exc:
        log.debug("Age block digit pass failed: %s", exc)
        return []
    return [digits for digits in re.findall(r"\d{2,3}", text) if AGE_MIN <= int(digits) <= AGE_MAX]


def _age_value_span(gray: Any) -> Any:
    """Crop the digits sitting between the age label and the gender label.

    Returns ``None`` when the labels cannot be located, which tells the
    caller to fall back to reading the whole age/gender band.
    """
    enlarged = cv2.resize(gray, None, fx=AGE_SPAN_UPSCALE, fy=AGE_SPAN_UPSCALE,
                          interpolation=cv2.INTER_CUBIC)
    data = pytesseract.image_to_data(
        enlarged, lang=TESSERACT_HINDI_LANG, config=TESSERACT_CONFIG,
        output_type=pytesseract.Output.DICT,
    )
    label = gender = None
    for i, text in enumerate(data["text"]):
        word = text.strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if confidence < 0:
            continue
        x, y = data["left"][i], data["top"][i]
        if label is None and AGE_LABEL_RE.search(word):
            label = (x, y, x + data["width"][i], y + data["height"][i])
        elif label is not None and gender is None and GENDER_LABEL_RE.search(word):
            gender = (x, y, x + data["width"][i], y + data["height"][i])
    if label is None:
        return None
    scale = AGE_SPAN_UPSCALE
    left, top = label[2] // scale, max(0, label[1] // scale - AGE_SPAN_PAD_PX)
    right = (gender[0] // scale if gender else gray.shape[1])
    bottom = min(gray.shape[0], label[3] // scale + AGE_SPAN_PAD_PX)
    if right - left < AGE_MIN_SPAN_PX:
        return None
    return gray[top:bottom, left:right]


def _read_age_digits(crop: Any, *, allow_block_mode: bool = False) -> str:
    """Read one cropped age value. Returns "" unless 18..120 is read."""
    if crop is None or getattr(crop, "size", 0) == 0:
        return ""
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    enlarged = cv2.resize(crop, None, fx=AGE_DIGIT_UPSCALE, fy=AGE_DIGIT_UPSCALE,
                          interpolation=cv2.INTER_CUBIC)
    # Tesseract needs a margin to place the line box; without it a short
    # two-digit crop comes back empty rather than short.
    enlarged = cv2.copyMakeBorder(enlarged, AGE_DIGIT_BORDER_PX, AGE_DIGIT_BORDER_PX,
                                  AGE_DIGIT_BORDER_PX, AGE_DIGIT_BORDER_PX,
                                  cv2.BORDER_REPLICATE)
    configs = AGE_DIGIT_CONFIGS
    if allow_block_mode:
        configs = configs + (
            "--oem 3 --psm 6 -l eng -c tessedit_char_whitelist=0123456789",
        )
    for config in configs:
        try:
            text = pytesseract.image_to_string(enlarged, config=config)
        except Exception as exc:
            log.debug("Age digit pass failed (%s): %s", config, exc)
            continue
        for digits in re.findall(r"\d{1,3}", text):
            if AGE_MIN <= int(digits) <= AGE_MAX:
                return digits
    return ""


def _read_age_from_lower_band(band: Any) -> str:
    """Read the lower age/gender row after the tighter readers failed.

    Some cards have an address continuation in the top of GENDER_AGE_REGION.
    Reading the whole band in PSM 6 can therefore select an address digit.
    Restrict the block-mode fallback to its lower half, where the printed
    ``आयु : NN  लिंग : ...`` row sits, and retain the normal voter-age bounds.
    """
    if band is None or getattr(band, "size", 0) == 0:
        return ""
    # Some cards place the age baseline slightly above the literal midpoint;
    # 45% retains that baseline while still excluding the address continuation
    # occupying the top of the band.
    start = int(band.shape[0] * 0.45)
    return _read_age_digits(band[start:, :], allow_block_mode=True)


def _GENDER_BAND_VARIANTS(gray: Any):
    """Pre-processed views of the age/gender band, best-yield first.

    Yield was measured over the live blank-gender records rather than assumed:
    adaptive thresholding was the single most productive pass, the plain
    upscale next, and sharpening/Otsu/blur mostly repeat what the upscale
    already got. They are kept because each rescues a different card, but they
    are tried after the passes that earn their keep most often.
    """
    up = cv2.resize(gray, None, fx=GENDER_BAND_UPSCALE, fy=GENDER_BAND_UPSCALE,
                    interpolation=cv2.INTER_CUBIC)
    yield cv2.adaptiveThreshold(
        up, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
        GENDER_BAND_ADAPT_WINDOW, GENDER_BAND_ADAPT_C,
    )
    yield up
    yield cv2.filter2D(up, -1, GENDER_BAND_SHARPEN_KERNEL)
    _, otsu = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu
    yield cv2.GaussianBlur(up, (3, 3), 0)


def _extract_gender_from_clean_card(card_img: Any) -> str:
    """Read only the age/gender row from an already cleaned card image.

    This is a recovery reader, not a competing full-card parser.  It lets a
    border or a merged name/relation line fail independently without losing a
    clear, closed-vocabulary gender value.  It is also intentionally reusable
    by the one-time repair job for old records.

    Primary pass: full card parse via parse_voter_box_from_ocr_lines.
    Fallback: upscale the band and do direct substring search for the Hindi
    gender tokens.  Tesseract sometimes fuses the लिंग label into the value
    (e.g. ``हिंगैभहिला`` or ``हिगचरुष``), but the distinctive endings survive
    even in garbled output.

    The fallback tests तृतीय लिंग first: it is the longest value, so on a band
    where the value is garbled but the लिंग label is still legible, matching
    on it early avoids reading the trailing लिंग as part of a different word.
    """
    try:
        crop = _crop_card_region(card_img, GENDER_AGE_REGION, "GENDER_AGE")
        if crop is None or crop.size == 0:
            return ""
        ok, encoded = cv2.imencode(".png", crop)
        if not ok:
            return ""
        lines, _ = _extract_text_with_tesseract(encoded.tobytes())
        value = str(parse_voter_box_from_ocr_lines(lines or []).get("gender") or "")
        if value:
            return value

        # Fallback ladder. The band is re-read through progressively stronger
        # pre-processing until the STRICT vocabulary matches. Accepting a
        # fuzzy match instead is not an option: every tolerant पुरुष pattern
        # tried matched real names (शत्रुघन, पुरुषोत्तम, खुरशेद, रूश्मा --
        # 32 to 82 false positives across the 113,616 names in the database),
        # because र..ष is a common shape and a wrong gender on a voter record
        # is worse than a blank one. Improving the image is what works: on a
        # 25-card sample of blank genders this ladder recovers 11, where the
        # plain upscale recovered 3, and every recovery is an exact match.
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        for variant in _GENDER_BAND_VARIANTS(gray):
            bordered = cv2.copyMakeBorder(variant, 12, 12, 12, 12,
                                          cv2.BORDER_REPLICATE)
            for cfg in GENDER_BAND_CONFIGS:
                try:
                    text = pytesseract.image_to_string(bordered, config=cfg)
                except Exception:
                    continue
                if not text.strip():
                    continue
                # तृतीय लिंग is matched through the same tolerant pattern the
                # parser uses, so the doubled त in तृत्तीय is still recognised.
                compact = re.sub(r"[\s:;ः,.|]", "", text)
                if THIRD_GENDER_RE.search(compact) or "third" in text.lower():
                    return "तृतीय लिंग"
                # Exact matches next, then distinctive suffix fallback.
                if "महिला" in text:
                    return "महिला"
                if "पुरुष" in text:
                    return "पुरुष"
                # Garbled: label fused into value; distinctive endings survive.
                if "हिला" in text:
                    return "महिला"
                if "रुष" in text:
                    return "पुरुष"
        return ""
    except Exception as exc:
        log.debug("Focused gender extraction failed: %s", exc)
        return ""


_VOTER_NAME_FIELDS = (
    "voter_first_name", "voter_middle_name", "voter_sur_name",
)
_RELATION_NAME_FIELDS = (
    "voter_father_name", "voter_husband_name",
    "voter_mother_name", "voter_other_name",
)


def _merge_missing_name_relation_fields(
    record: dict[str, Any], recovery: dict[str, Any],
) -> bool:
    """Fill only absent voter/relation values from a clearer re-render.

    The fast page-slice image and the direct card image are two renderings of
    the same source pixels. The direct image is a recovery source only: it
    never replaces a non-empty value accepted from the fast path.
    """
    changed = False
    if not any(str(record.get(field) or "").strip() for field in _VOTER_NAME_FIELDS):
        for field in _VOTER_NAME_FIELDS:
            value = str(recovery.get(field) or "").strip()
            if value:
                record[field] = value
                changed = True
    if not any(str(record.get(field) or "").strip() for field in _RELATION_NAME_FIELDS):
        for field in _RELATION_NAME_FIELDS:
            value = str(recovery.get(field) or "").strip()
            if value:
                record[field] = value
                record["relation_name"] = str(recovery.get("relation_name") or "")
                changed = True
                break
    return changed


# Printed house placeholders. The display layer already turns these stored
# glyphs into "Not provided in roll". A blank stays a blank, so a missed
# read is not presented as a printed dash.
_HOUSE_NOT_PROVIDED_VALUE = "-"
_HOUSE_PLACEHOLDER_GLYPHS = frozenset({"-", "—", "–", "«", ","})

# Direct-card fallback counters. The fast path does not touch these; they
# move only when a house or an age was still empty after the slice readers.
_RECOVERY_STATS = {
    "house_invocations": 0,
    "age_invocations": 0,
    "house_recovered": 0,
    "house_placeholder": 0,
    "house_unresolved": 0,
    "house_false_digit_blocked": 0,
    "age_recovered": 0,
    "age_unresolved": 0,
    "age_disagreements": 0,
}
_HOUSE_RECOVERY_LOCKS = frozenset({
    "direct_card_consensus",
    "source_placeholder_detected",
    "unresolved_after_direct_card",
    "english_line_consensus",
})
_AGE_RECOVERY_LOCKS = frozenset({
    "direct_card_consensus",
    "unresolved_after_direct_card",
})


def recovery_stats_reset() -> None:
    for key in _RECOVERY_STATS:
        _RECOVERY_STATS[key] = 0


def recovery_stats_snapshot() -> dict[str, int]:
    return dict(_RECOVERY_STATS)


def _direct_field_recovery_enabled() -> bool:
    return os.getenv("HOUSE_AGE_DIRECT_RECOVERY", "on") != "off"


def _house_recovery_locked(record: dict[str, Any]) -> bool:
    source = (record.get("_field_sources") or {}).get("house_number", "")
    return source in _HOUSE_RECOVERY_LOCKS


def _age_recovery_locked(record: dict[str, Any]) -> bool:
    if _valid_stored_age(record.get("age")):
        return True
    source = (record.get("_field_sources") or {}).get("age", "")
    return source in _AGE_RECOVERY_LOCKS


def _valid_stored_age(value: Any) -> bool:
    text = str(value or "").strip()
    return text.isdigit() and AGE_MIN <= int(text) <= AGE_MAX


def _same_house_token(left: str, right: str) -> bool:
    return re.sub(r"\s+", "", left) == re.sub(r"\s+", "", right)


def _house_token_from_ocr_text(text: str) -> str:
    """One house candidate from a single OCR string, or empty.

    A printed dash, and a string that is only punctuation, are not
    candidates. A lone digit is returned so the arbiter can reject it;
    it is never treated as corroborated by itself.
    """
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    if not raw or raw in _HOUSE_PLACEHOLDER_GLYPHS:
        return ""
    if re.fullmatch(r"[-—–«.,:;/|\s]+", raw):
        return ""
    if re.search(rf"[{_DEV_LETTER}]", raw) and not re.search(r"\d", raw):
        cleaned = raw.strip(" :ः;,.|-—–")
        if _devanagari_consonants(cleaned) >= 2:
            return cleaned
        return ""
    slash = re.search(r"(\d{1,5})\s*/\s*(\d{1,4})", raw)
    if slash:
        return f"{slash.group(1)}/{slash.group(2)}"
    runs = re.findall(r"\d+", raw)
    if not runs:
        return ""
    return max(runs, key=len)


def _house_is_symbol_soup(value: str) -> bool:
    """True when a house value is symbol noise rather than a printed address.

    Hindi OCR turns an English locality into brackets and digits
    (``KHASRA NUMBER`` becomes ``1(1+578182``). A real Hindi address and a
    plain number are not soup. ``23(डी)`` stays, because the Devanagari is
    the value.
    """
    text = str(value or "").strip()
    if not text or re.search(rf"[{_DEV_LETTER}]", text):
        return False
    if re.search(r"[A-Za-z]{3}", text):
        return False
    return bool(re.search(r"[()\[\]{}+#%&|॥]", text))


def _house_english_tail_needs_reread(raw: Optional[str]) -> bool:
    """The English house read already saw a word, so the digits-only value is incomplete."""
    text = str(raw or "")
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    if re.search(r"[A-Za-z]{4}", text):
        return True
    return bool(re.search(r"[A-Za-z]{2}\s+[A-Za-z]{2}", text))


def _normalize_english_house_line(text: str) -> str:
    """Keep the house tail of one English read: letters, digits, slash, hyphen."""
    raw = str(text or "").replace("\n", " ")
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    raw = raw.upper()
    raw = re.sub(r"[^A-Z0-9/,\- ]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" ,-/")
    return raw


def _strip_house_tail_noise(tokens: list[str]) -> list[str]:
    """Drop a trailing EE or CX. A real token such as KH or the block letter B stays."""
    noise = {"EE", "CX", "FE", "AE", "OE", "AA", "OO"}
    cleaned = list(tokens)
    while cleaned and cleaned[-1] in noise:
        cleaned.pop()
    return cleaned


def _house_tokens_split_a_digit(tokens: list[str]) -> bool:
    for index in range(len(tokens) - 1):
        if re.fullmatch(r"\d", tokens[index]) and re.match(r"\d", tokens[index + 1]):
            return True
    return False


def _agree_english_house_text(first: str, second: str) -> str:
    """Return the house line only when two English reads are the same line.

    Trailing letter-noise may differ. ``1 24 KHASRA NUMBER`` and
    ``124 KHASRA NUMBER`` are the same line. ``KHOLI`` and ``KHOL`` are not,
    and that case returns nothing rather than a guess. A state name read
    from the page header is not a house.
    """
    left = _strip_house_tail_noise(_normalize_english_house_line(first).split())
    right = _strip_house_tail_noise(_normalize_english_house_line(second).split())
    if not left or not right:
        return ""
    shared = 0
    while shared < len(left) and shared < len(right) and left[shared] == right[shared]:
        shared += 1
    if shared == len(left) == len(right):
        chosen = left
    elif shared and shared == min(len(left), len(right)):
        longer = left if len(left) > len(right) else right
        extra = longer[shared:]
        if extra and all(re.fullmatch(r"[A-Z]{1,3}", token) for token in extra):
            chosen = longer[:shared]
        else:
            chosen = []
    else:
        chosen = []
    if not chosen:
        folded_left = re.sub(r"[^A-Z0-9]", "", " ".join(left))
        folded_right = re.sub(r"[^A-Z0-9]", "", " ".join(right))
        if folded_left and folded_left == folded_right:
            intact = [tokens for tokens in (left, right) if not _house_tokens_split_a_digit(tokens)]
            pool = intact or [left, right]
            chosen = max(pool, key=len)
    if not chosen:
        return ""
    agreed = " ".join(chosen).strip()
    if re.sub(r"[^A-Z]", "", agreed) == "UTTARPRADESH":
        return ""
    return agreed


def _agreed_literal_house(card_img: Any) -> str:
    """Two English reads of the whole house line. Nothing is returned on a split."""
    crop = _crop_card_region(card_img, (0.12, 0.46, 0.99, 0.64), "HOUSE_ENGLISH")
    if crop is None or getattr(crop, "size", 0) == 0:
        return ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    enlarged = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    reads: list[str] = []
    for config in ("--oem 3 --psm 7 -l eng", "--oem 3 --psm 6 -l eng"):
        try:
            _tess_count("house_english_line")
            reads.append(pytesseract.image_to_string(enlarged, config=config))
        except Exception as exc:
            log.debug("English house line read failed: %s", exc)
            return ""
    if len(reads) != 2:
        return ""
    return _agree_english_house_text(reads[0], reads[1])


def _replace_symbol_soup_house(card_img: Any, record: dict[str, Any], english_raw: Optional[str]) -> None:
    """Replace a misread English house, or leave it blank. Never keep the soup.

    Runs only when the current value is symbol noise, or the English read
    already showed a word such as KHASRA. A plain number is left alone.
    """
    current = str(record.get("house_no") or "")
    if re.search(rf"[{_DEV_LETTER}]", current):
        return
    if not _house_is_symbol_soup(current) and not _house_english_tail_needs_reread(english_raw):
        return
    literal = _agreed_literal_house(card_img)
    sources = record.setdefault("_field_sources", {})
    if literal:
        record["house_no"] = literal
        sources["house_number"] = "english_line_consensus"
        return
    if _house_is_symbol_soup(current):
        record["house_no"] = ""
        sources["house_number"] = "unresolved_after_direct_card"
        _mark_review(record, "house_symbol_soup_unresolved")


def _detect_house_not_provided(card_img: Any) -> bool:
    """True when the house-value crop is an isolated dash, not a digit.

    The crop is the band to the right of ``मकान संख्या``. A printed ``-``
    is a flat stroke. A digit is taller than it is wide. Specks are
    ignored, and a crop with no ink at all stays unresolved rather than
    being called a dash.
    """
    # Slightly above and left of the value crop. On some scans the hyphen
    # sits on the top edge of HOUSE_VALUE_REGION and would otherwise be clipped.
    crop = _crop_card_region(
        card_img, (0.18, 0.42, 0.662, 0.60), "HOUSE_PLACEHOLDER",
    )
    if crop is None or getattr(crop, "size", 0) == 0:
        return False
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    # The printed hyphen is a short, light stroke just after the label.
    # A threshold of 170 keeps only its darkest core and the mark disappears.
    ink = (gray < 210).astype(np.uint8) * 255
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    dashes = 0
    glyphs = 0
    for index in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[index])
        if area < 5 or y <= 2:
            continue
        # A tall body is a digit or a letter. A tall mark glued to the left
        # edge is the house label bleeding into the value crop, not a value.
        if height >= 8 and height >= width * 0.45 and x > 2:
            glyphs += 1
            continue
        if 3 <= width <= 22 and 1 <= height <= 6 and width >= height:
            dashes += 1
    return dashes >= 1 and glyphs == 0


def _arbitrate_house_reads(placeholder: bool, reads: list[str]) -> tuple[str, str]:
    """Classify direct house reads.

    Returns ``(value_recovered|not_provided_in_roll|unresolved, value)``.
    A single digit never overrides a detected dash, and a value with only
    one supporting read is left unresolved.
    """
    tokens = [token for token in (_house_token_from_ocr_text(r) for r in reads) if token]
    strong = [token for token in tokens if not re.fullmatch(r"\d", token)]
    if placeholder:
        if len(strong) >= 2 and all(_same_house_token(strong[0], token) for token in strong[1:]):
            return "value_recovered", strong[0]
        # A dash crop that OCR turned into 7, or into 7257, is not a house.
        if tokens:
            _RECOVERY_STATS["house_false_digit_blocked"] += 1
        return "not_provided_in_roll", _HOUSE_NOT_PROVIDED_VALUE
    agreed = [token for token in tokens if sum(_same_house_token(token, other) for other in tokens) >= 2]
    if agreed:
        candidate = agreed[0]
        # The two numeric reads are deliberately different pre-processings,
        # but they still originate from the same tiny crop.  On a printed
        # dash both can hallucinate the same one-character digit.  A missing
        # field is safer than inventing a house number, so exact-card fallback
        # never creates a one-digit house number.  Existing values from the
        # normal extractor are untouched; this applies only to recovery.
        if re.fullmatch(r"\d", candidate):
            _RECOVERY_STATS["house_false_digit_blocked"] += 1
            return "unresolved", ""
        return "value_recovered", candidate
    return "unresolved", ""


def _numeric_house_signals(crop: Any) -> list[str]:
    """Two independent English reads of one house-value crop."""
    if crop is None or getattr(crop, "size", 0) == 0:
        return []
    scale = max(2, -(-TESSERACT_MIN_HEIGHT_PX // max(1, crop.shape[0])))
    scaled = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY) if scaled.ndim == 3 else scaled
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    signals: list[str] = []
    try:
        _tess_count("house_direct_raw")
        raw = _tess_string(
            Image.fromarray(gray), lang=TESSERACT_ENGLISH_LANG,
            config="--oem 3 --psm 7",
        )
        token = _house_token_from_ocr_text(raw)
        if token:
            signals.append(token)
    except Exception as exc:
        log.debug("Direct house raw read failed: %s", exc)
    try:
        _tess_count("house_direct_otsu")
        text = _tess_string(
            Image.fromarray(otsu), lang=TESSERACT_ENGLISH_LANG,
            config=TESSERACT_HOUSE_CONFIG,
        )
        token = _house_token_from_ocr_text(text)
        if token:
            signals.append(token)
    except Exception as exc:
        log.debug("Direct house otsu read failed: %s", exc)
    return signals


def _labelled_house_signals(card_img: Any) -> list[str]:
    """Hindi house text from the label band and the value band, separately."""
    found: list[str] = []
    for region, label in (
        (HOUSE_REGION, "DIRECT_HOUSE"),
        (HOUSE_VALUE_REGION, "DIRECT_HOUSE_VALUE"),
    ):
        crop = _crop_card_region(card_img, region, label)
        if crop is None or crop.size == 0:
            continue
        ok, encoded = cv2.imencode(".png", crop)
        if not ok:
            continue
        lines, _ = _extract_text_with_tesseract(encoded.tobytes())
        value = _labelled_house_text(lines if isinstance(lines, list) else None)
        if value:
            found.append(value)
    return found


def _house_value_segment(text: str) -> str:
    """The part of one OCR line that follows the house label.

    ``मकान संख्या : 301`` and ``Ha: 61`` both put the value after the last
    separator that still has a digit. A line with no separator is the value.
    """
    raw = str(text or "").replace("\n", " ")
    segment = ""
    for part in re.split(r"[:ः=]", raw):
        if re.search(r"\d", part):
            segment = part
    return segment or raw


def _house_tail_token(text: str) -> str:
    """One house from a single line, or empty when the line is not one house.

    ``A 19`` keeps the plot letter. ``301`` and ``95/11`` stay as printed.
    A leftover ``a.`` after the number is dropped. Two different numbers,
    a lone digit, and the state name read from the header are not a house.
    """
    upper = _house_value_segment(text).upper()
    folded = re.sub(r"[^A-Z]", "", upper)
    if "UTTARPRADESH" in folded and not re.search(r"\d", upper):
        return ""
    cleaned = re.sub(r"[^A-Z0-9/]", " ", upper)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    tokens = _strip_house_tail_noise(cleaned.split())
    # Trailing scraps are the leftover of ``a.`` and ``pp ct``, not a plot
    # letter. A leading ``A`` in ``A 19`` is not trailing, so it stays.
    while tokens and re.fullmatch(r"[A-Z]{1,2}", tokens[-1]):
        tokens.pop()
    tokens = _strip_house_tail_noise(tokens)
    multi = [token for token in tokens if re.fullmatch(r"\d{2,6}(?:/\d{1,4})?", token)]
    singles = [token for token in tokens if re.fullmatch(r"\d", token)]
    # One house number. A following ``0`` or ``7`` is label noise (``151 ee 0``).
    # Letter scraps (``eee``, ``KHASRA``) do not erase that number; a word line
    # that two readings share is returned earlier, intact.
    if len(multi) == 1:
        number = multi[0]
    elif not multi and len(singles) == 1:
        number = singles[0]
    else:
        return ""
    letters = [token for token in tokens if re.fullmatch(r"[A-Z]+", token)]
    # A real word (``KHASRA``, ``BLOCK``, ``PANCHVATI``) is the address.
    # Reducing it to the bare number, or to a trailing ``2``, is a different
    # house. Short scraps (``eee``, ``aga``) are not a word.
    if any(len(word) >= 4 for word in letters):
        return ""
    # ``A 19`` is a plot letter plus the number. ``a eee 2 171`` is not:
    # the leading ``a`` is label noise beside another word.
    if letters == [tokens[0]] and len(tokens[0]) == 1 and len(number) >= 2:
        return f"{tokens[0]} {number}"
    return number


def _agreed_house_tail(card_img: Any) -> str:
    """The house line when two readings of it are the same house.

    The tight digit crop misses a number that sits on the label. The wider
    line still has it. Gray and a second binarisation, plus a single-line
    pass, each vote. One reading, or two different numbers, returns nothing.
    """
    crop = _crop_card_region(card_img, (0.12, 0.46, 0.99, 0.64), "HOUSE_ENGLISH")
    if crop is None or getattr(crop, "size", 0) == 0:
        return ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    enlarged = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, otsu = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    reads: list[str] = []
    for image, config in (
        (enlarged, "--oem 3 --psm 6 -l eng"),
        (otsu, "--oem 3 --psm 6 -l eng"),
        (enlarged, "--oem 3 --psm 7 -l eng"),
    ):
        try:
            _tess_count("house_english_tail")
            reads.append(pytesseract.image_to_string(image, config=config))
        except Exception as exc:
            log.debug("House tail read failed: %s", exc)
            return ""
    segments = [_house_value_segment(text) for text in reads]
    for left, right in ((segments[0], segments[1]), (segments[0], segments[2]), (segments[1], segments[2])):
        agreed = _agree_english_house_text(left, right)
        if agreed and re.search(r"[A-Za-z]{3}", agreed) and not re.fullmatch(r"\d", agreed):
            return agreed
    tokens = [_house_tail_token(text) for text in reads]
    present = [token for token in tokens if token]
    if len(present) < 2:
        return ""
    counts: dict[str, int] = {}
    for token in present:
        counts[token] = counts.get(token, 0) + 1
    best = max(counts, key=counts.get)
    # Two readings of the same house are enough. A third reading that
    # dropped a digit (``821`` read once as ``21``) does not cancel them.
    # A two-way split (``1646`` against ``1648``) has no such pair.
    if counts[best] < 2:
        return ""
    return best


def _numeric_house_blocks(token: str, numeric: list[str]) -> bool:
    """True when two digit-crop readings agreed on a different house.

    A single reading does not veto the house line, and neither does a lone
    digit. Disagreeing readings do not veto either: that split is the extra
    leading ``1`` (``191`` coming back as ``1191``).
    """
    distinct = list(dict.fromkeys(item for item in numeric if item))
    # One digit-crop reading is not a house on its own, so it cannot veto
    # two readings of the house line. A lone digit cannot veto either:
    # that crop is the dash the one-digit rule already refuses to store.
    if len(numeric) < 2 or len(distinct) != 1 or re.fullmatch(r"\d", distinct[0]):
        return False
    other = distinct[0]
    if other == token:
        return False
    body = token.split()[-1]
    if (
        re.fullmatch(r"\d+", other)
        and re.fullmatch(r"\d+", body)
        and other.endswith(body)
        and 0 < len(other) - len(body) <= 2
    ):
        return False
    return True


def _slash_was_read_as_one(stroke: str, scaled: str) -> bool:
    """True when a printed slash was read as an extra ``1``.

    ``311/9`` comes back from the strokes as ``31119``. The line still has
    the slash. The stroke digits are that house with one or two extra ones
    and nothing else changed.
    """
    if "/" not in scaled or not re.fullmatch(r"\d+", stroke):
        return False
    body = scaled.replace("/", "")
    if not re.fullmatch(r"\d+", body):
        return False
    index = 0
    extras = 0
    for char in stroke:
        if index < len(body) and char == body[index]:
            index += 1
        elif char == "1":
            extras += 1
        else:
            return False
    return index == len(body) and 1 <= extras <= 2


def _choose_agreed_house_value(stroke: str, scaled: str, letter: str, has_word: bool) -> str:
    """Pick one house from a stroke read and a two-scale line read.

    A trailing ``+`` means the last stroke was not a digit. That prefix is
    kept only when the line is the same digits plus one extra ``0`` (the
    unread stroke read as zero). A word on the line is an address, and a
    bare number is not substituted for it.
    """
    if has_word:
        return ""
    trailing = stroke.endswith("+")
    body = stroke[:-1] if trailing else stroke
    if trailing:
        if body and scaled == body + "0":
            return f"{body} {letter}".strip() if letter else body
        return ""
    if body and scaled and body != scaled:
        if _slash_was_read_as_one(body, scaled):
            return scaled
        return ""
    if body:
        return body
    return scaled or ""


def _value_ink_pieces(gray: Any, ink: Any) -> list[tuple[int, int, int, int]]:
    """Tall strokes in the house-value crop, ignoring the sliced label edge."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    height = gray.shape[0]
    pieces = []
    for index in range(1, count):
        x, y, width, glyph_height, area = (int(value) for value in stats[index])
        # x == 0 is the house label cut by the crop, not the first digit.
        # A digit that starts a few pixels in (the ``3`` of ``327``) stays.
        if area < 8 or y <= 1 or x == 0 or glyph_height < height * 0.35:
            continue
        pieces.append((x, y, width, glyph_height))
    return sorted(pieces)


def _read_house_piece(gray: Any, x: int, y: int, width: int, glyph_height: int) -> Optional[str]:
    """One stroke as digits. A thin vertical stroke is a printed ``1``."""
    if width <= glyph_height * 0.50:
        return "1"
    glyph = gray[max(0, y - 2):y + glyph_height + 2, max(0, x - 2):x + width + 2]
    if glyph.size == 0:
        return None
    enlarged = cv2.resize(glyph, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    enlarged = cv2.copyMakeBorder(enlarged, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    found = []
    for config in (
        "--oem 3 --psm 10 -l eng -c tessedit_char_whitelist=0123456789",
        "--oem 3 --psm 8 -l eng -c tessedit_char_whitelist=0123456789",
    ):
        try:
            _tess_count("house_value_piece")
            text = pytesseract.image_to_string(enlarged, config=config)
        except Exception as exc:
            log.debug("House value piece failed: %s", exc)
            return None
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            found.append(digits)
    if not found or len(set(found)) != 1:
        return None
    piece = found[0]
    if re.fullmatch(r"\d{1,3}", piece):
        return piece
    return None


def _stroke_house(gray: Any) -> str:
    """The house both ink thresholds draw, or empty.

    A trailing ``+`` marks a last stroke that is not a digit. The caller
    keeps that prefix only when the line read explains the extra stroke.
    """
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    fixed = ((gray < 170).astype("uint8")) * 255
    groups = [_value_ink_pieces(gray, otsu), _value_ink_pieces(gray, fixed)]
    if not groups[0] or len(groups[0]) != len(groups[1]) or not 1 <= len(groups[0]) <= 7:
        return ""
    strings = []
    trailing_failed = False
    for pieces in groups:
        chars: list[str] = []
        failed = False
        for index, piece in enumerate(pieces):
            read = _read_house_piece(gray, *piece)
            if read is None:
                if index == len(pieces) - 1 and chars:
                    failed = True
                    break
                return ""
            chars.append(read)
        if failed:
            trailing_failed = True
        strings.append("".join(chars))
    if strings[0] != strings[1]:
        return ""
    value = strings[0]
    if trailing_failed:
        return value + "+"
    if re.fullmatch(r"\d", value) and value != "1":
        return ""
    if re.fullmatch(r"\d{1,6}", value):
        return value
    return ""


def _scaled_house_token(gray: Any) -> tuple[str, bool]:
    """The house two enlargements of the value crop share, and whether a word is there."""
    tokens = []
    has_word = False
    for scale in (2, 3):
        enlarged = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        try:
            _tess_count("house_value_scale")
            text = pytesseract.image_to_string(enlarged, config="--oem 3 --psm 7 -l eng")
        except Exception as exc:
            log.debug("House value scale failed: %s", exc)
            return "", False
        upper = str(text or "").upper()
        if re.search(r"[A-Z]{4}", upper):
            has_word = True
        segment = upper
        for part in re.split(r"[:ः=]", upper):
            if re.search(r"\d", part):
                segment = part
        if "UTTAR" in re.sub(r"[^A-Z]", "", segment):
            tokens.append("")
            continue
        slashed = re.search(r"[A-Z]\d{1,4}/\d{1,4}|\d{2,6}/\d{1,4}", segment)
        number = re.search(r"\d{2,6}", segment)
        tokens.append(slashed.group(0) if slashed else (number.group(0) if number else ""))
    if tokens[0] and tokens[0] == tokens[1]:
        return tokens[0], has_word
    return "", has_word


def _hindi_house_letter(gray: Any, digits: str) -> str:
    """A Devanagari letter printed straight after the house digits, or empty."""
    enlarged = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    try:
        _tess_count("house_value_hindi")
        text = pytesseract.image_to_string(enlarged, config="--oem 3 --psm 7 -l hin+eng")
    except Exception as exc:
        log.debug("Hindi house letter failed: %s", exc)
        return ""
    match = re.search(rf"{re.escape(digits)}\s*([{_DEV_LETTER}])", text or "")
    return match.group(1) if match else ""


def _house_from_value_agreement(crop: Any) -> str:
    """A house the value crop's strokes and two line scales can both support.

    Used when the wider house line was noise. A word address is left alone.
    A stroke the line turned into an extra ``0``, or a slash the strokes
    turned into ``1``, is kept in the form the other read supports.
    """
    if crop is None or getattr(crop, "size", 0) == 0:
        return ""
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    stroke = _stroke_house(gray)
    scaled, has_word = _scaled_house_token(gray)
    letter = ""
    if stroke.endswith("+") and scaled == stroke[:-1] + "0":
        letter = _hindi_house_letter(gray, stroke[:-1])
    return _choose_agreed_house_value(stroke, scaled, letter, has_word)


def _classify_house_on_card(card_img: Any) -> tuple[str, str]:
    """Recover a house, recognise a printed dash, or leave the field open."""
    placeholder = _detect_house_not_provided(card_img)
    crop = _crop_card_region(card_img, HOUSE_VALUE_REGION, "DIRECT_HOUSE_VALUE")
    if placeholder:
        return _arbitrate_house_reads(True, _numeric_house_signals(crop))
    signals = _numeric_house_signals(crop)
    kind, value = _arbitrate_house_reads(False, signals)
    if kind == "value_recovered":
        return kind, value
    labelled = _labelled_house_signals(card_img)
    kind, value = _arbitrate_house_reads(False, labelled)
    if kind == "value_recovered":
        return kind, value
    # The digit crop was empty or split. The house line is used only when
    # two readings name the same house and the digit crop did not
    # unanimously name a different one. A lone digit stays unread.
    tail = _agreed_house_tail(card_img)
    if tail and not _numeric_house_blocks(tail, signals):
        return "value_recovered", tail
    # The line was noise, but the strokes are still there. Read each tall
    # stroke on its own. Both ink thresholds have to produce the same
    # number. A slash house the strokes do not agree on stays blank.
    separated = _house_from_separated_digits(crop)
    if separated and not _numeric_house_blocks(separated, signals):
        return "value_recovered", separated
    # The line was noise. The value crop still has the strokes. Both ink
    # thresholds, or two enlargements of that crop, have to name one house.
    agreed = _house_from_value_agreement(crop)
    if agreed and not _numeric_house_blocks(agreed, signals):
        return "value_recovered", agreed
    return kind, value


def _house_glyphs(gray: Any, ink: Any) -> str:
    """Read the tall strokes of one binarisation as a house, or return ""."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    height = gray.shape[0]
    pieces = []
    for index in range(1, count):
        x, y, width, glyph_height, area = (int(value) for value in stats[index])
        if area < 8 or y <= 1:
            continue
        tall = glyph_height >= height * 0.35 and glyph_height >= width * 0.4
        slash = 2 <= glyph_height <= 10 and width >= glyph_height and width <= 18
        if tall or slash:
            pieces.append((x, y, width, glyph_height, slash and not tall))
    if not 2 <= len(pieces) <= 7:
        return ""
    configs = (
        "--oem 3 --psm 10 -l eng -c tessedit_char_whitelist=0123456789/",
        "--oem 3 --psm 8 -l eng -c tessedit_char_whitelist=0123456789/",
    )
    chars = []
    for x, y, width, glyph_height, is_slash in sorted(pieces):
        if is_slash:
            chars.append("/")
            continue
        glyph = gray[max(0, y - 2):y + glyph_height + 2, max(0, x - 2):x + width + 2]
        if glyph.size == 0:
            return ""
        enlarged = cv2.resize(glyph, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
        enlarged = cv2.copyMakeBorder(enlarged, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
        found = []
        for config in configs:
            try:
                _tess_count("house_separated_digit")
                text = pytesseract.image_to_string(enlarged, config=config)
            except Exception as exc:
                log.debug("Separated house digit failed: %s", exc)
                return ""
            digits = "".join(ch for ch in text if ch.isdigit() or ch == "/")
            if len(digits) == 1:
                found.append(digits)
        if not found or len(set(found)) != 1:
            return ""
        chars.append(found[0])
    value = "".join(chars).strip("/")
    if re.fullmatch(r"\d{2,6}", value) or re.fullmatch(r"\d{1,5}/\d{1,4}", value):
        return value
    return ""


def _house_from_separated_digits(crop: Any) -> str:
    """The house number when two binarisations read the same strokes.

    A thin digit disappears in a line read (``581`` comes back as ``81``).
    Splitting the strokes keeps it, but only when both thresholds agree.
    One digit is not a house here: a dash is one stroke.
    """
    if crop is None or getattr(crop, "size", 0) == 0:
        return ""
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    fixed = ((gray < 170).astype("uint8")) * 255
    separated = _house_glyphs(gray, otsu)
    if separated and separated == _house_glyphs(gray, fixed):
        return separated
    return ""


def _arbitrate_age_reads(reads: list[str], tight: str = "") -> tuple[str, str]:
    """Accept one age only when the valid reads agree.

    ``19`` against ``49`` stays unresolved. One lone read stays unresolved
    unless a separate tight digit crop produced that same age from two
    preprocessing passes and nothing else contradicts it.
    """
    valid = [text for text in (str(item or "").strip() for item in reads) if _valid_stored_age(text)]
    distinct = set(valid)
    if len(distinct) > 1:
        return "unresolved", ""
    if len(valid) >= 2:
        return "value_recovered", valid[0]
    confirmed = str(tight or "").strip()
    if confirmed and _valid_stored_age(confirmed) and (not distinct or distinct == {confirmed}):
        return "value_recovered", confirmed
    return "unresolved", ""


def _digits_from_age_ink(gray: Any, ink: Any) -> str:
    """Join the tall strokes of one binarisation into an age, or return ""."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    height = gray.shape[0]
    pieces = []
    for index in range(1, count):
        x, y, width, glyph_height, area = (int(value) for value in stats[index])
        if glyph_height < height * 0.45 or area < 8:
            continue
        pieces.append((x, y, width, glyph_height))
    if not 2 <= len(pieces) <= 3:
        return ""
    configs = (
        "--oem 3 --psm 10 -l eng -c tessedit_char_whitelist=0123456789",
        "--oem 3 --psm 8 -l eng -c tessedit_char_whitelist=0123456789",
    )
    digits = []
    for x, y, width, glyph_height in sorted(pieces):
        crop = gray[max(0, y - 2):y + glyph_height + 2, max(0, x - 2):x + width + 2]
        if crop.size == 0:
            return ""
        enlarged = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
        enlarged = cv2.copyMakeBorder(enlarged, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
        found_digits = []
        for config in configs:
            try:
                _tess_count("age_separated_digit")
                text = pytesseract.image_to_string(enlarged, config=config)
            except Exception as exc:
                log.debug("Separated age digit failed: %s", exc)
                return ""
            found = "".join(character for character in text if character.isdigit())
            if len(found) == 1:
                found_digits.append(found)
        # Two modes that both see a digit have to see the same one.
        # ``5`` read as ``3`` by one mode is left unread.
        if not found_digits or len(set(found_digits)) != 1:
            return ""
        digits.append(found_digits[0])
    age = "".join(digits)
    return age if _valid_stored_age(age) else ""


def _age_from_separated_digits(span: Any) -> str:
    """Read the age one glyph at a time from the label-bounded span.

    A thin printed ``1`` disappears when the whole span is read as one
    line, so ``41`` comes back as ``4`` and is then rejected as too small.
    The span is already the gap between ``आयु`` and ``लिंग``. Two
    binarisations have to split that gap into the same digits.
    """
    if span is None or getattr(span, "size", 0) == 0:
        return ""
    gray = span if span.ndim == 2 else cv2.cvtColor(span, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    fixed = ((gray < 170).astype("uint8")) * 255
    separated = _digits_from_age_ink(gray, otsu)
    if separated and separated == _digits_from_age_ink(gray, fixed):
        return separated
    return ""


def _tight_age_consensus(card_img: Any) -> str:
    """Read age only from the value span bounded by ``आयु`` and ``लिंग``.

    The prior connected-component approach was not tied to the printed
    labels.  On a shifted card it could select digits from a neighbouring
    field.  No labels means no tight fallback: the normal readers may still
    provide a consensus, otherwise the age remains unresolved.
    """
    band = _crop_card_region(card_img, GENDER_AGE_REGION, "DIRECT_AGE_TIGHT")
    if band is None or getattr(band, "size", 0) == 0:
        return ""
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY) if band.ndim == 3 else band
    span = _age_value_span(gray)
    if span is None or getattr(span, "size", 0) == 0:
        return ""
    _, otsu = cv2.threshold(span, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(
        span, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 8,
    )
    reads = [
        _read_age_digits(candidate, allow_block_mode=False)
        for candidate in (span, otsu, adaptive)
    ]
    valid = [value for value in reads if _valid_stored_age(value)]
    # Any contradiction is a reason to leave the age open. Otherwise two
    # preprocessing paths must independently see the same digits.
    if len(set(valid)) == 1 and len(valid) >= 2:
        return valid[0]
    # The line reader dropped a thin 1. The helper itself requires two
    # binarisations of this label-bounded span to agree.
    return _age_from_separated_digits(span)


def _collect_age_reads(card_img: Any) -> list[str]:
    """Independent age reads. None of them is allowed to win by itself."""
    band = _crop_card_region(card_img, GENDER_AGE_REGION, "DIRECT_AGE")
    if band is None or getattr(band, "size", 0) == 0:
        return []
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY) if band.ndim == 3 else band
    reads: list[str] = []
    span = _age_value_span(gray)
    if span is not None and getattr(span, "size", 0):
        spanned = _read_age_digits(span, allow_block_mode=False)
        if spanned:
            reads.append(spanned)
    lower = _read_age_from_lower_band(band)
    if lower:
        reads.append(lower)
    up = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    up = cv2.copyMakeBorder(up, 10, 10, 10, 10, cv2.BORDER_REPLICATE)
    try:
        text = pytesseract.image_to_string(up, config="--oem 3 --psm 6 -l hin")
    except Exception:
        text = ""
    match = re.search(r"(?:आयु|आवु|आय|आग|आगु)[^०-९0-9]{0,6}([0-9]{2,3})", text or "")
    if match and _valid_stored_age(match.group(1)):
        reads.append(match.group(1))
    return reads


def _classify_age_on_card(card_img: Any) -> tuple[str, str]:
    reads = _collect_age_reads(card_img)
    return _arbitrate_age_reads(reads, _tight_age_consensus(card_img))


def _render_exact_card(page: fitz.Page, card_rect: fitz.Rect, dpi: int = 200) -> Any:
    """Rasterise one card from its PDF rectangle, not from a page slice."""
    png = page.get_pixmap(dpi=dpi, clip=card_rect, alpha=False).tobytes("png")
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return None
    return _remove_outer_card_lines(image)


def _recover_missing_house_and_age_from_direct_card(
    page: fitz.Page, card_rect: fitz.Rect, record: dict[str, Any],
) -> None:
    """Fill a missing house or age from the exact card, or leave it blank.

    Runs only when the fast slice left the field empty. An existing value
    is never replaced. A printed dash is stored as ``-`` so the display
    layer can say "Not provided in roll". Anything uncertain stays blank.
    """
    if not _direct_field_recovery_enabled():
        return
    need_house = not str(record.get("house_no") or "").strip()
    need_age = not _valid_stored_age(record.get("age")) and not str(record.get("age") or "").strip()
    if not need_house and not need_age:
        return
    try:
        image = _render_exact_card(page, card_rect)
    except Exception as exc:
        log.debug("Exact card render failed: %s", exc)
        return
    if image is None:
        return
    sources = record.setdefault("_field_sources", {})
    if need_house:
        _RECOVERY_STATS["house_invocations"] += 1
        kind, value = _classify_house_on_card(image)
        if kind == "value_recovered" and value:
            record["house_no"] = value
            sources["house_number"] = "direct_card_consensus"
            _RECOVERY_STATS["house_recovered"] += 1
        elif kind == "not_provided_in_roll":
            record["house_no"] = _HOUSE_NOT_PROVIDED_VALUE
            sources["house_number"] = "source_placeholder_detected"
            _RECOVERY_STATS["house_placeholder"] += 1
        else:
            sources["house_number"] = "unresolved_after_direct_card"
            _mark_review(record, "house_unresolved_after_direct_card")
            _RECOVERY_STATS["house_unresolved"] += 1
    if need_age:
        _RECOVERY_STATS["age_invocations"] += 1
        reads = _collect_age_reads(image)
        tight = _tight_age_consensus(image)
        kind, value = _arbitrate_age_reads(reads, tight)
        # A few thin printed 1s are below the reliable resolution at 200 DPI.
        # Escalate only after the normal, label-bounded exact-card pass has no
        # consensus. The 300-DPI read remains subject to the same two-read
        # agreement rule; it is evidence, not a guess.
        if kind == "unresolved":
            try:
                high_res = _render_exact_card(page, card_rect, dpi=300)
            except Exception as exc:
                log.debug("High-resolution exact age render failed: %s", exc)
                high_res = None
            if high_res is not None:
                high_reads = _collect_age_reads(high_res)
                high_tight = _tight_age_consensus(high_res)
                kind, value = _arbitrate_age_reads(
                    reads + high_reads, high_tight,
                )
        if kind == "value_recovered" and value:
            record["age"] = value
            sources["age"] = "direct_card_consensus"
            _RECOVERY_STATS["age_recovered"] += 1
        else:
            sources["age"] = "unresolved_after_direct_card"
            distinct = {item for item in reads if _valid_stored_age(item)}
            if _valid_stored_age(tight):
                distinct.add(tight)
            reason = (
                "age_direct_readers_disagree"
                if len(distinct) > 1
                else "age_unresolved_after_direct_card"
            )
            _mark_review(record, reason)
            _RECOVERY_STATS["age_unresolved"] += 1
            if len(distinct) > 1:
                _RECOVERY_STATS["age_disagreements"] += 1


def _recover_missing_name_relation_from_direct_card(
    page: fitz.Page, card_rect: fitz.Rect, record: dict[str, Any],
) -> bool:
    """Read names from an exact PDF-card render only when the fast read missed.

    Full-page rendering is much faster for normal cards, but integer slice
    boundaries can clip a small number of top name/relation rows. The direct
    render uses the PDF rectangle itself and is therefore used as a bounded
    fallback rather than replacing the normal fast path.
    """
    needs_voter = not any(
        str(record.get(field) or "").strip() for field in _VOTER_NAME_FIELDS
    )
    needs_relation = not any(
        str(record.get(field) or "").strip() for field in _RELATION_NAME_FIELDS
    )
    if not (needs_voter or needs_relation):
        return False
    try:
        png = page.get_pixmap(dpi=200, clip=card_rect, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return False
        cleaned = _remove_outer_card_lines(image)
        crop = _crop_card_region(
            cleaned, TESSERACT_CARD_CONTENT_REGIONS[2], "DIRECT_NAME_RECOVERY"
        )
        if crop is None or crop.size == 0:
            return False
        ok, encoded = cv2.imencode(".png", crop)
        if not ok:
            return False
        lines, _ = _extract_text_with_tesseract(encoded.tobytes())
        return _merge_missing_name_relation_fields(
            record, parse_voter_box_from_ocr_lines(lines or [])
        )
    except Exception as exc:
        log.debug("Direct card name/relation recovery failed: %s", exc)
        return False


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
        # The small box to the right of the serial. Empty on a normal card.
        serial_mark = _extract_serial_mark(cl)
        _t("serial mark", serial_mark)

        # ── 4. EPIC / Voter ID ───────────────────────────────────────────
        id_card_no = ""
        epic_png = _crop_png(cl, TESSERACT_CARD_CONTENT_REGIONS[EPIC_REGION_INDEX], "EPIC")
        # Clear the scratch slot before the read so a stale split from a prior
        # card on this thread cannot attach itself to this one.
        _EPIC_VOTE_SPLIT.data = None
        if epic_png:
            e_reads = _extract_epic_with_tesseract(epic_png)
            _t("_extract_epic_with_tesseract", e_reads)
            if not (e_reads and e_reads[0]):
                e_reads = _extract_english_text_with_tesseract(epic_png)
                _t("_extract_english_text_with_tesseract (EPIC fallback)", e_reads)
            if e_reads and e_reads[0]:
                id_card_no = e_reads[0]
        _t("EPIC → id_card_no", id_card_no)
        # Stash the split here; `record` does not exist until the Hindi parse
        # below, so it cannot be applied yet.
        epic_split = _EPIC_VOTE_SPLIT.data

        # ── 5. Hindi fields ──────────────────────────────────────────────
        hindi_fields_lines = None
        hindi_fields_conf = None
        hindi_png = _crop_png(cl, TESSERACT_CARD_CONTENT_REGIONS[2], "HINDI_FIELDS")
        if hindi_png:
            hindi_fields_lines, hindi_fields_conf = _extract_text_with_tesseract(hindi_png)
            _t("_extract_text_with_tesseract (hindi crop)", f"lines={hindi_fields_lines} conf={hindi_fields_conf}")
        if not hindi_fields_lines:
            ok, enc_cl = cv2.imencode(".png", cl)
            if ok:
                hindi_fields_lines, hindi_fields_conf = _extract_text_with_tesseract(enc_cl.tobytes())
                _t("_extract_text_with_tesseract (full card fallback)", f"lines={hindi_fields_lines} conf={hindi_fields_conf}")

        # ── 6. Parse Hindi lines into record ─────────────────────────────
        record = parse_voter_box_from_ocr_lines(hindi_fields_lines or [])
        if hindi_fields_conf is not None:
            record["_tesseract_conf"] = hindi_fields_conf
        _t("parse_voter_box_from_ocr_lines", record)
        if record.get("empty"):
            record = _empty_record()
            _t("_empty_record (parse returned empty)", record)

        # Only when relation parsing failed (or a known distorted label was
        # seen), re-read the original crop and merge a recognised relation.
        # Existing voter fields are never replaced by this fallback.
        relation_fields = (
            "voter_father_name", "voter_husband_name",
            "voter_mother_name", "voter_other_name",
        )
        relation_label_distorted = any("प्रित्ा" in line for line in (hindi_fields_lines or []))
        if relation_label_distorted or not any(str(record.get(field) or "").strip() for field in relation_fields):
            raw_hindi_png = _crop_png(card_img, TESSERACT_CARD_CONTENT_REGIONS[2], "RAW_HINDI_FIELDS")
            if raw_hindi_png:
                raw_lines, _ = _extract_text_with_tesseract(raw_hindi_png)
                raw_record = parse_voter_box_from_ocr_lines(raw_lines or [])
                for field in relation_fields:
                    value = str(raw_record.get(field) or "").strip()
                    if value:
                        record[field] = value
                        record["relation_name"] = raw_record.get("relation_name", "")
                        _t("raw relation fallback", {field: value})
                        break

        # The normal full-page render is retained for speed. A slice boundary
        # can however clip the top name/relation line on a small minority of
        # cards. Re-render only those incomplete cards from the exact PDF
        # rectangle and merge only the missing values.
        if _recover_missing_name_relation_from_direct_card(page, card_rect, record):
            _t("direct name/relation recovery", {
                "voter": " ".join(
                    str(record.get(field) or "") for field in _VOTER_NAME_FIELDS
                ).strip(),
                "relation": next((
                    str(record.get(field) or "")
                    for field in _RELATION_NAME_FIELDS if record.get(field)
                ), ""),
            })

        # A name printed in Telugu or Kannada is not a blank and not Hindi
        # garbage. Read that script, then store the same word in Devanagari.
        # English is derived from that Hindi later. An empty label stays empty.
        relation_in_hindi = any(
            re.search(r"[अ-हक़-य़]", str(record.get(field) or ""))
            for field in relation_fields
        )
        if not relation_in_hindi:
            for field in relation_fields:
                record[field] = ""
            kind, hindi_name = _read_foreign_relation(cl)
            field = _FOREIGN_RELATION_FIELD.get(kind)
            if field and hindi_name:
                record["relation_name"] = kind
                record[field] = hindi_name
                record.setdefault("_field_sources", {})["relative_name"] = (
                    "foreign_script_as_hindi"
                )
                _t("foreign relation as Hindi", {field: hindi_name})

        # The full Hindi block can lose the final age/gender row where a card
        # border crosses it.  Re-read that one bounded row only when gender
        # is missing; this avoids changing good card output while recovering
        # the closed male/female value from a clearer crop.
        if not record.get("gender"):
            focused_gender = _extract_gender_from_clean_card(cl)
            if focused_gender:
                record["gender"] = focused_gender
                _t("focused gender fallback", record["gender"])

        # Same story for age: a Devanagari two-digit value arrives as one
        # digit and is dropped by the 18..120 floor, so recover it from the
        # label-bounded span when -- and only when -- age is missing.
        if not record.get("age"):
            focused_age = _extract_age_from_clean_card(cl)
            if focused_age:
                record["age"] = focused_age
                _t("focused age fallback", record["age"])

        if sno:
            record["sno"] = sno
        if serial_mark:
            record["serial_mark"] = serial_mark
        if id_card_no:
            record["id_card_no"] = id_card_no

        # Apply the EPIC split now that `record` exists. The chosen value is
        # left untouched -- a disagreement asks for a human check, it does not
        # authorise substituting a different voter's ID.
        if epic_split:
            record["_needs_review"] = True
            record.setdefault("_review_reasons", []).append(
                "epic_readers_disagree: "
                + ", ".join(f"{c['value']}x{c['votes']}" for c in epic_split)
            )
            record.setdefault("_field_sources", {})["id_card_no"] = (
                "tesseract_split_vote"
            )

        # ── 7. House number ──────────────────────────────────────────────
        try:
            h_lines = None
            house_png = _crop_png(cl, HOUSE_REGION, "HOUSE")
            if house_png:
                h_lines, _ = _extract_text_with_tesseract(house_png)
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
                hv_lines, _ = _extract_text_with_tesseract(hv_png)
                _t("_extract_text_with_tesseract (HOUSE_VAL region)", hv_lines)
                e_digit, e_raw = _extract_house_number_with_tesseract_both(hv_png)
                _t("_extract_house_number_with_tesseract (digits)", e_digit)
                _t("_extract_house_number_with_tesseract (raw)", e_raw)
                # PaddleOCR for house deferred to main thread (not thread-safe)
            # If HV returned nothing, try PaddleOCR on the wider HOUSE crop,
            # then fall back to Tesseract — catches single-digit values like
            # '1' that Tesseract LSTM misses on narrow glyphs.
            if not e_digit and house_png and not house_is_text:
                e_digit = _extract_house_number_with_tesseract(house_png)
                _t("_extract_house_number_with_tesseract (HOUSE fallback)", e_digit)

            # Extract just the lines for _combine_house_read (ignores confidence)
            # hindi_fields_lines is either None, a list[str], or tuple[list[str], float]
            hindi_lines = hindi_fields_lines[0] if isinstance(hindi_fields_lines, tuple) else hindi_fields_lines
            h_lines_only = h_lines[0] if isinstance(h_lines, tuple) else h_lines
            hv_lines_only = hv_lines[0] if isinstance(hv_lines, tuple) else hv_lines
            combined = _combine_house_read(hindi_lines, e_digit, h_lines_only, hv_lines_only, e_raw)
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
            else:
                # A clear, explicitly labelled Devanagari house value (for
                # example "मकान संख्या : विष्णु") is not a malformed numeric
                # house number.  Preserve it rather than converting it into a
                # false blank merely because the numeric arbitration has no
                # digit run to combine.
                labelled_text = _labelled_house_text(h_lines_only)
                if not labelled_text:
                    labelled_text = _labelled_house_text(hv_lines_only)
                if labelled_text:
                    record["house_no"] = labelled_text
                    _t("house_no (labelled Hindi text recovery)", labelled_text)
            _replace_symbol_soup_house(cl, record, e_raw)
        except Exception as exc:
            log.debug("House number extraction failed: %s", exc)

        # The slice can clip the house or age row. Re-render that card from
        # its own PDF rectangle only when the field is still empty, and only
        # keep a value the readers agree on.
        _recover_missing_house_and_age_from_direct_card(page, card_rect, record)

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
        # A card is a real voter if it has a name OR a well-formed EPIC.
        #
        # The old test was truthiness over five fields, which blank boxes
        # defeated: an empty box still OCRs its grid-rule fragments into short
        # junk ("BS" serial, "OS" voter id), and a non-empty string is truthy.
        # 68 such boxes were saved as if they were people.
        #
        # "Name OR valid EPIC" cannot drop a genuine voter. Every named card in
        # the reference run carries a shape-valid EPIC, and a card whose name
        # failed still keeps its ID -- the two failure modes are disjoint. A
        # deleted card ("Q" prefix, struck through) is the clearest case: the
        # horizontal rules cut the Devanagari name to pieces while leaving the
        # 3-letter + 7-digit ID legible, because that text has wide gaps between
        # glyphs and no continuous shirorekha. Both kept. Blank boxes have
        # neither, and fail the format test by a wide margin.
        has_name = any(
            str(record.get(k) or "").strip()
            for k in ("voter_first_name", "voter_middle_name", "voter_sur_name")
        )
        epic = str(record.get("id_card_no") or "").strip().upper()
        if not has_name and not EPIC_SHAPE_RE.match(epic):
            _t("FINAL RECORD", "→ discarded (blank box: no name, no valid EPIC)")
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

def _compute_confidence(
    tesseract_conf: Optional[float],
    paddle_conf: Optional[dict[str, float]]
) -> tuple[Optional[float], Optional[float]]:
    """Combine Tesseract and PaddleOCR confidences into a 0-1 score.

    Returns (confidence, coverage).

    Tesseract confidence is 0-100 (word-level mean).
    PaddleOCR confidence is 0-1 per field (serial, house, age).

    Two separate numbers, because they answer different questions:

    - **confidence** -- of the text that was actually read, how much do we
      trust it?
    - **coverage** -- what fraction of the card did we even attempt, and
      how much of that produced a usable read?

    Reporting confidence alone cannot distinguish "read everything and got
    it right" from "read one field, got it right." Both are a mean over
    the same single correct value, so both came back as 1.0.

    Zeros are evidence. PaddleOCR records a field it could not read as 0.0
    (see the `if field_confs else 0.0` in the sequential pass), and that is
    a *worse* result than a low-confidence read, not an absent one. The
    previous version filtered those zeros out before averaging --

        valid_confs = [c for c in paddle_conf.values() if c > 0]

    -- which rewarded failure twice over. A card PaddleOCR recognised
    nothing on scored as well as one it recognised everything on, because
    the fields that failed contributed nothing to the mean. And when every
    field scored 0.0 the filtered list came back empty, `paddle_avg`
    became None, and the function returned `tess_norm` at full weight: a
    total Paddle failure was indistinguishable from Paddle never running,
    and both were indistinguishable from Paddle agreeing.

    So a field's presence in the dict is the record of whether we *tried*
    it, and its value is the record of what we *got*:

    - key absent  -> not attempted; no evidence either way.
    - key = 0.0   -> attempted and missed; drags the mean and the coverage.

    Coverage is the fraction of attempted fields that returned a read. It
    is reported alongside the score rather than folded into it, because a
    single number cannot carry both meanings: weighting a missed field at
    zero drags the average down, but it drags it the same way whether the
    card had one field or five, so a card read badly-but-broadly and a card
    read well-but-briefly both land mid-scale.

    Coverage is measured over the Paddle fields alone, and is None when
    there are none. Tesseract is excluded on purpose: we know *that* it
    read, but not how many fields it should have found, so counting it as
    one unit would let it dilute the ratio with a number we cannot
    subdivide -- and would report 1.0, "everything was read", for a card
    where a whole engine contributed nothing. None means "no per-field
    evidence", which is the honest answer there.

    Returns (None, None) when neither engine produced anything at all --
    there is no evidence to be confident or unconfident about.
    """
    # Normalise Tesseract's 0-100 into 0-1. Clamped because the raw value is
    # a mean of Tesseract's own per-word scores and a malformed one would
    # otherwise leave the response outside the 0-1 contract that
    # normalize._confidence() has to clamp on the way into the database.
    #
    # NaN is checked before the clamp, not by the clamp. `min(1.0, nan)` and
    # `max(0.0, nan)` are both nan in CPython, and `min` returns its first
    # argument when the comparison is False, so `max(0.0, min(1.0, nan))`
    # quietly yields 0.0 -- which would report a read we could not evaluate
    # as the *worst possible* result rather than as no result at all. A
    # garbage Tesseract score is no evidence, so the engine drops out
    # entirely and Paddle alone carries the result.
    tess_norm: Optional[float] = None
    if tesseract_conf is not None:
        try:
            number = float(tesseract_conf)
        except (TypeError, ValueError):
            number = None
        if number is not None and number == number:  # not NaN
            tess_norm = max(0.0, min(1.0, number / 100.0))

    # Mean over every field we attempted, zeros included. Not filtered.
    paddle_avg: Optional[float] = None
    paddle_fields = 0
    paddle_hits = 0
    if isinstance(paddle_conf, dict) and paddle_conf:
        values = []
        for value in paddle_conf.values():
            # An unparseable value is not "not attempted" -- it is a number
            # we could not read, so it counts as a field we tried and did
            # not get. Dropping it would shrink the denominator and quietly
            # re-create the mean-over-successes that this function exists to
            # stop computing.
            number = 0.0
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = 0.0
            if number != number or number in (float("inf"), float("-inf")):
                # NaN and the infinities are valid floats that survive
                # float(), so they reach the clamp. NaN would come out the
                # other side as 0.0 and infinity as 1.0 -- and a bare clamp
                # turning "the producer emitted infinity" into the *maximum*
                # possible confidence is the most misleading value available.
                # normalize._confidence() rejects these for the same reason.
                number = 0.0
            values.append(max(0.0, min(1.0, number)))
        if values:
            paddle_fields = len(values)
            paddle_hits = sum(1 for v in values if v > 0)
            paddle_avg = sum(values) / len(values)

    if tess_norm is not None and paddle_avg is not None:
        confidence = (tess_norm + paddle_avg) / 2.0
    elif tess_norm is not None:
        confidence = tess_norm
    elif paddle_avg is not None:
        confidence = paddle_avg
    else:
        return None, None

    if paddle_fields == 0:
        return confidence, None
    return confidence, paddle_hits / paddle_fields


# ── Review triggers ────────────────────────────────────────────────────────
#
# Until now `_needs_review` was set in exactly one place: inside `if
# epic_split:`. Every other field had no path to a flag at all, so 3 of 531
# records were marked and the flag measured the EPIC vote rather than the
# pipeline's uncertainty. That undercounts -- a record can be unflagged simply
# because nobody ever looked at it.
#
# These triggers do NOT correct anything. They only say "a human should look at
# this field", which is a different claim from "this value is wrong". Each one
# fires on an observable condition rather than on a confidence score, because a
# score is not a threshold anyone has ever validated against real errors.
#
# The rules are deliberately conservative. A trigger that fires on healthy
# records trains people to ignore the flag, which costs more than a miss: the
# one signal this system has for "check me" stops meaning anything.
#
#   age_absent        no age at all -- either engine found no digits
#   age_out_of_range  age outside 18..120 -- the two in-voter bounds
#   age_not_integer   age is present but not a plain integer
#   house_absent      no house number at all
#   house_devanagari  house carries Devanagari that is neither a known label
#                     word nor a single-consonant plot letter -- i.e. OCR junk
#                     the arbitration did not resolve.
#   house_leading_1   a leading 1 was PREPENDED rather than read. This is a
#                     reconstruction: the digits now present include one no
#                     read ever saw. See below.
#
# Why leading-1 is flagged. `if paddle_house == "1" + cur_digits` at the merge
# site fabricates a digit to reconcile two engines. It is right often enough to
# keep, but the value it returns was never printed on the card -- so it needs a
# human more than any other value does, not less.

AGE_MIN, AGE_MAX = 18, 120

# House values that are Devanagari by design rather than by OCR failure.
#
# The roll prints free-text addresses, and two requirements pull in opposite
# directions on them:
#   "we can ignore these values हाऊस ने॑ एचएनओ ... all we want is number"
#   "we want english letters preserved like E represented as choti e ki matra"
# So neither shape is an error: label words are noise to be dropped, and a
# letter beside a number is data to be kept. Flagging either as a defect would
# put a large share of CORRECT records into the review queue.
#
# What IS a defect is Devanagari that is neither -- unknown junk. That is the
# only house shape this flags, and it is why the count is small.
_HOUSE_FILLER_TOKENS = (
    "हाऊस", "हाउस", "मकान", "एचएनओ", "एचएनजीओ", "प्लॉट", "प्लाट", "ख", "नं",
    "न॑", "ने॑", "संख्या", "वार्ड", "मोहल्ला", "पता", "गली", "कॉलोनी", "सं",
    "सं0", "ब्लॉक", "ब्लाक", "फ्लैट", "फ्लेंट", "फ्लोर", "तल", "मंजिल",
)

# Devanagari code points that ATTACH to a consonant rather than being one:
# matras, the virama, nukta, and the accents. Excluded from the consonant
# count, or 'बी' (b + ii-matra) counts as two consonants and every plot letter
# in the roll reads as a word.
#
# Independent vowels (U+0905–U+0914: अ आ इ ई उ ऊ ऋ ॠ ऌ ॡ ए ऐ ओ औ) are also
# excluded. They appear as vowel carriers in transliterated block/floor letters
# such as 'एच' (H), 'एफ' (F), 'आर' (R). Counting them as consonants caused
# those single-letter plot codes to read as two-consonant runs and be flagged
# as junk. The fix: extend the mark range from ऀ (U+0900) through औ (U+0914)
# so independent vowels are skipped exactly like matras.
_DEVA_RUN = re.compile(r"[ऀ-ॿ]+")
_DEVA_MARK = re.compile(r"[ऀ-औऺ-॑ॕॢॣ]")

# Punctuation is replaced by a SPACE, never deleted outright. Deleting it
# glues two separate plot letters together and the result reads as a word:
#   'पी. नं-बी 190, ख नं-701'  -> deleting '.', '-' and ',' leaves
#                                'पीबी'  -> 2 consonants -> flagged as junk.
# That value is plot 190 / khasra 701, a real two-part address the user
# confirmed. Space-separated, 'पी' and 'बी' are each one consonant, which is
# what makes them plot letters.
_DEVA_GLUE = re.compile(r"[\s.,\-–/:]+")


def _devanagari_consonants(run: str) -> int:
    """Count consonants in a Devanagari run, ignoring marks.

    'बी' -> 1 (a plot letter). 'व्दव' -> 3 (v, virama, d, v -- junk). The
    virama is the case that decides this: counting it made the confirmed-bad
    'व्दव 1141' classify as a single plot letter, so the rule missed the one
    house defect that has actually been confirmed against the pixels.
    """
    return sum(1 for ch in run if not _DEVA_MARK.match(ch))


def _legal_devanagari_conjunct(first: str, second: str) -> bool:
    """A virama the rolls actually print inside a place name.

    प्र, क्ल, पार्क and मुस्तफा are normal. व्द and छ्ल are the broken
    clusters the review rule was written to catch.
    """
    if first in "छझठढणञ":
        return False
    if first == "र" or second in "रयलव":
        return True
    if first == second:
        return True
    if (first, second) in {("क", "ष"), ("ज", "ञ")}:
        return True
    if first in "सशष":
        return True
    if first in "नमङञ" and second not in "अआइईउऊएऐओऔ":
        return True
    return False


def _run_is_place_word(run: str) -> bool:
    """A multi-consonant run that is a Hindi word, not OCR salad.

    अशोक, विहार, नगर and प्रेम are addresses. तर, व्दव, छ्लो and क्रज are
    the shapes this queue is for. A word needs a vowel, or three consonants
    and no virama (नगर). A virama has to be one of the conjuncts above.
    """
    if "\u094d" in run:
        chars = list(run)
        index = 0
        while index < len(chars):
            if chars[index] == "\u094d":
                return False
            if index + 1 < len(chars) and chars[index + 1] == "\u094d":
                if index + 2 >= len(chars):
                    return False
                if not _legal_devanagari_conjunct(chars[index], chars[index + 2]):
                    return False
                # क्ष्मी is two conjuncts sharing ष. Stay on that letter
                # when it is itself the start of the next one.
                if index + 3 < len(chars) and chars[index + 3] == "\u094d":
                    index += 2
                else:
                    index += 3
                continue
            index += 1
    has_vowel = re.search(r"[अ-औा-ौॉॅँंः]", run) is not None
    if has_vowel:
        return True
    return "\u094d" not in run and _devanagari_consonants(run) >= 3


def _classify_devanagari_house(house: str) -> tuple[str, list[str]]:
    """Split a Devanagari house value into (verdict, leftover runs).

    verdict:
      "clean"   only label words remained -- 'हाऊस नं- 453'
      "plot"    plot letters or a real Hindi address -- 'इ-8/496',
                'अशोक विहार', 'प्रेम नगर'. Correct values, kept.
      "unknown" broken Devanagari -- 'व्दव 1141', 'तर 42', 'छ्लो-9'.
                That is the only house shape worth a review flag.

    A locality name has more than one consonant, so the old rule flagged
    every real address. The broken clusters are still flagged.
    """
    residue = house
    for token in sorted(_HOUSE_FILLER_TOKENS, key=len, reverse=True):
        # Whole token only. A substring replace turns संगम into गम because
        # the label word सं sits inside it, and गम then looks like junk.
        residue = re.sub(
            r"(?<![ऀ-ॿ])" + re.escape(token) + r"(?![ऀ-ॿ])",
            " ",
            residue,
        )
    residue = _DEVA_GLUE.sub(" ", residue)
    runs = _DEVA_RUN.findall(residue)
    if not runs:
        return "clean", []
    unknown = [
        run for run in runs
        if _devanagari_consonants(run) > 1 and not _run_is_place_word(run)
    ]
    if unknown:
        return "unknown", unknown
    return "plot", runs


def _mark_review(record: dict[str, Any], reason: str) -> None:
    """Attach a review reason to a record, once, without touching its values.

    Deliberately separate from any correction path. Every caller in this file
    uses it the same way: decide, then ask for a human. A trigger that also
    rewrote the value would make "flagged" mean "changed", and the two are not
    the same claim.
    """
    record["_needs_review"] = True
    reasons = record.setdefault("_review_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _review_record(record: dict[str, Any]) -> None:
    """Run every review trigger over one finished record. Adds flags only.

    Called once per record, after all OCR and arbitration is complete, so a
    trigger sees the value that will actually be emitted rather than an
    intermediate that later gets replaced.
    """
    # ── age ──
    age = record.get("age", "")
    if age is None or str(age).strip() == "":
        _mark_review(record, "age_absent")
    else:
        age_text = str(age).strip()
        if not age_text.isdigit():
            _mark_review(record, "age_not_integer")
        else:
            age_int = int(age_text)
            if not (AGE_MIN <= age_int <= AGE_MAX):
                _mark_review(
                    record,
                    "age_out_of_range: %d not in %d..%d"
                    % (age_int, AGE_MIN, AGE_MAX),
                )

    # ── house ──
    house = record.get("house_no", "")
    house_text = str(house).strip() if house is not None else ""
    if not house_text:
        _mark_review(record, "house_absent")
    elif re.search(r"[ऀ-ॿ]", house_text):
        # Devanagari alone is not a defect: most of it is correct (see the
        # filler-token comment above). Only the unknown shape is flagged.
        verdict, runs = _classify_devanagari_house(house_text)
        if verdict == "unknown":
            _mark_review(
                record,
                "house_devanagari_unknown: %s in %r"
                % ("/".join(runs[:3]), house_text),
            )

    # ── the reconstructed leading 1 ──
    if record.get("_house_leading_1_prepended"):
        _mark_review(record, "house_leading_1_prepended: %r" % house_text)


def _public_record(
    record: dict[str, Any], counter: int, pdf_name: str,
    roll_metadata: dict[str, str],
) -> dict[str, Any]:
    """Convert the internal OCR record to the public API schema."""
    # Multi-section rolls carry an authoritative page-specific selection from
    # the cover list.  Single-section and legacy paths continue to use the
    # request-wide metadata object.
    effective_roll_metadata = record.get("_roll_metadata") or roll_metadata
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

    confidence, coverage = _compute_confidence(
        record.get("_tesseract_conf"),
        record.get("_paddle_conf")
    )

    metadata_review_reasons = [
        reason for reason in (
            roll_metadata.get("anubhag_review_reason", ""),
            roll_metadata.get("state_review_reason", ""),
        ) if reason
    ]
    metadata_review_reason = ",".join(metadata_review_reasons)
    review_reasons = list(record.get("_review_reasons", []))
    for reason in metadata_review_reasons:
        if reason not in review_reasons:
            review_reasons.append(reason)

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
        # The second box beside the serial on an addition-list card.
        # "Not printed" means the card has no such box.
        "serial_mark": str(record.get("serial_mark") or "Not printed"),
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
        "state_code": effective_roll_metadata.get("state_code", ""),
        "ac_code": effective_roll_metadata.get("ac_code", ""),
        "anubhag_code": effective_roll_metadata.get("anubhag_code", ""),
        "anubhag_name": effective_roll_metadata.get("anubhag_name", ""),
        "ward_code": (
            effective_roll_metadata.get("ward_code", "")
            or (
                ""
                if effective_roll_metadata.get("roll_format") == "municipal_ward"
                else "Not on this roll"
            )
        ),
        "roll_format": effective_roll_metadata.get("roll_format", "assembly"),
        "anubhag_status": effective_roll_metadata.get("anubhag_status", "applicable"),
        "booth_code": effective_roll_metadata.get("booth_code", ""),
        "pdf_name": pdf_name,
        # A roll-wide header applies to every card.  If it could not be read
        # safely, surface that fact on each resulting record instead of
        # producing silent blanks that look like valid data downstream.
        "needs_review": bool(record.get("_needs_review", False) or metadata_review_reason),
        "review_reasons": review_reasons,
        "field_sources": dict(record.get("_field_sources", {})),
        # Confidence: combine Tesseract and PaddleOCR confidences.
        # `confidence` is how much to trust the text that was read;
        # `confidence_coverage` is how much of the card was read at all.
        # A high score over a thin read is not a good extraction, and one
        # number cannot say both things -- see _compute_confidence().
        "confidence": confidence,
        "confidence_coverage": coverage,
    }

# How many of the twelve probed card positions must yield an EPIC-shaped read
# before a page is accepted as a voter grid.
#
# Measured on HIN-565 (33 pages, 300-card roll): a real card page answered
# 12/12, while the trailing summary page -- no cards on it at all -- answered
# 2/12, both of them junk ("77T55", "V"). Four sits between the two with room
# on each side: high enough to reject any page that does not actually print ID
# numbers in the grid, low enough that a real page survives even when a third
# of its boxes are blank or discarded slots.
#
# The previous test was a single hit, which the junk reads cleared. That is
# what let the summary page through as a page of thirty nameless, ageless
# phantom records. Those junk reads do not survive EPIC_SHAPE_RE (3 letters
# and 7 digits), so a shape-valid hit is a different, stricter event than the
# one that used to admit the summary page.
MIN_EPIC_HITS_PER_PAGE = 4

# Every third row, all three columns. A full card page hits nearly all of
# these. A section that ends after a row or two only fills the top of the
# grid, so this sample sees just the first row.
_GRID_PROBE_INDEXES = (0, 1, 2, 9, 10, 11, 18, 19, 20, 27, 28, 29)


def _probe_card_epic(page: fitz.Page, rect: fitz.Rect) -> bool:
    _tess_count("grid_probe_cards")
    image = getattr(page, "get_pixmap")(dpi=200, clip=rect, alpha=False).tobytes("png")
    return bool(_extract_tesseract_epic(image))


def _page_looks_like_voter_grid(page: fitz.Page) -> tuple[bool, int, int]:
    """Detect a voter-card grid by probing fixed card positions for real EPICs.

    Returns (is_voter_grid, hits, probed). `hits` counts positions that produced
    an EPIC-shaped read; `probed` is how many positions were examined.

    Two ways to pass, because the sample above skips rows 2 and 3:

    - Four or more hits across the twelve sampled positions. This is the
      full-page test, and it still rejects the summary page, which scores no
      shape-valid EPIC.
    - A run of shape-valid EPICs packed from the first card. A section that
      ends mid-page prints its last cards there and nowhere else. HIN-447
      page 28 has six of them (serials 751-756); the old sample only saw the
      first row, scored 3, and skipped the page, which is the whole of the
      932-versus-927 gap on that roll. The cover and the summary page have
      no such run.
    """
    card_rects = _voter_card_rects(page)
    probe_indexes = [i for i in _GRID_PROBE_INDEXES if i < len(card_rects)]
    if not probe_indexes:
        return False, 0, 0

    hit_at: dict[int, bool] = {}

    def probe(index: int) -> bool:
        if index not in hit_at:
            hit_at[index] = _probe_card_epic(page, card_rects[index])
        return hit_at[index]

    # The top row decides the short-page case on its own, and a full page
    # passes here too, so the common path probes three cards instead of twelve.
    for index in (0, 1, 2):
        if index >= len(card_rects):
            break
        probe(index)
    if all(hit_at.get(index) for index in (0, 1, 2)) and 2 < len(card_rects):
        return True, 3, 3

    for index in probe_indexes:
        probe(index)
    hits = sum(hit_at.values())
    if hits >= MIN_EPIC_HITS_PER_PAGE:
        return True, hits, len(hit_at)

    leading = 0
    for index in (0, 1, 2):
        if not hit_at.get(index):
            break
        leading += 1
    if leading:
        return True, leading, len(hit_at)
    return False, hits, len(hit_at)

# The same official catalogue ocr_text_pdf_api.py resolves state names
# against. Fetched lazily and cached, so a roll costs no external request
# while an ECI outage degrades to the printed token alone, never to a guess.
_ECI_STATES_URL = os.getenv(
    "ECI_STATES_URL", "https://gateway-voters.eci.gov.in/api/v1/common/states/")
_ECI_STATES_TIMEOUT_SECONDS = float(os.getenv("ECI_STATES_TIMEOUT_SECONDS", "5"))
_ECI_STATES_CACHE_SECONDS = int(os.getenv("ECI_STATES_CACHE_SECONDS", "86400"))
_ECI_STATES_FAILURE_CACHE_SECONDS = int(
    os.getenv("ECI_STATES_FAILURE_CACHE_SECONDS", "300"))
_ECI_STATES_LOCK = threading.Lock()
_ECI_STATES_CACHE: tuple[float, tuple[dict[str, str], ...], str] | None = None

# 300 DPI, not higher. At 1200 DPI the 4x-upscaled crop is so large that
# Tesseract's single-line mode returns nothing for every roll, and the code
# was stored blank on all of them.
_STATE_TOKEN_DPI = 300


def _active_eci_states() -> tuple[tuple[dict[str, str], ...], str]:
    """Return ECI's active state/UT catalogue, or an explicit failure reason.

    Each entry keeps the full ``S24``/``U05`` code as ``state_cd`` and its
    numeric part as ``code``, which is the shape the public field has always
    carried.
    """
    global _ECI_STATES_CACHE
    now = time.monotonic()
    with _ECI_STATES_LOCK:
        if _ECI_STATES_CACHE is not None and now < _ECI_STATES_CACHE[0]:
            return _ECI_STATES_CACHE[1], _ECI_STATES_CACHE[2]

        try:
            response = httpx.get(_ECI_STATES_URL, timeout=_ECI_STATES_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("ECI states response is not a list")

            states: list[dict[str, str]] = []
            for item in payload:
                # The endpoint stopped sending isActive at all, so requiring
                # "Y" discarded every state. Only an explicit "N" excludes one.
                if not isinstance(item, dict) or item.get("isActive") == "N":
                    continue
                state_cd = str(item.get("stateCd") or "").strip().upper()
                if not re.fullmatch(r"[SU]\d{2}", state_cd):
                    continue
                english = unicodedata.normalize("NFC", str(item.get("stateName") or "")).strip()
                hindi = unicodedata.normalize("NFC", str(item.get("stateNameHindi") or "")).strip()
                if not english and not hindi:
                    continue
                states.append({
                    "state_cd": state_cd,
                    "code": state_cd[1:],
                    "english": english,
                    "hindi": hindi,
                })
            if not states:
                raise ValueError("ECI states response has no active states")
        except Exception as exc:
            reason = f"state_catalogue_unavailable:{type(exc).__name__}"
            log.warning("Could not load official ECI state catalogue: %s", exc)
            _ECI_STATES_CACHE = (
                now + _ECI_STATES_FAILURE_CACHE_SECONDS, tuple(), reason)
            return tuple(), reason

        _ECI_STATES_CACHE = (now + _ECI_STATES_CACHE_SECONDS, tuple(states), "")
        return _ECI_STATES_CACHE[1], ""


def _state_from_text(text: str, states: tuple[dict[str, str], ...]) -> dict[str, str] | None:
    """Return exactly one state whose official ECI name appears in `text`.

    Exact matching, not fuzzy: a wrong state propagates to every voter in
    the roll, so two candidate names is no answer rather than a pick.
    """
    folded = unicodedata.normalize("NFC", text or "").casefold()
    matches: list[dict[str, str]] = []
    for state in states:
        for name in (state["english"], state["hindi"]):
            if name and name.casefold() in folded:
                matches.append(state)
                break
    unique = {match["state_cd"]: match for match in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _cover_header_clip(page: fitz.Page) -> fitz.Rect:
    # Proportional, not a fixed point count. The printed state token sits at
    # 4.7-6.3% of page height on both rolls measured, but page height is not
    # fixed: 2026-EROLLGEN-S24-53-HIN-300 is 842pt and this 65pt clip reaches
    # 7.7% of it, while 2026-EROLLGEN-S28-40-HIN-107 is 2806pt and the same
    # 65pt reaches only 2.3% -- cropping the blank space ABOVE the title and
    # returning '' with no error. _read_state_token then reported
    # state_code_unreadable and the record shipped an empty state/AC/booth
    # from a perfectly legible cover. The floor keeps a short page from
    # collapsing to nothing; 9% clears the measured 6.3%.
    return fitz.Rect(
        page.rect.x0, page.rect.y0,
        page.rect.x1, page.rect.y0 + max(65.0, page.rect.height * 0.09),
    )


def _read_state_token(page: fitz.Page) -> str:
    """Read the printed ``S24``-style token from the page-1 heading, or ''."""
    image_bytes = getattr(page, "get_pixmap")(
        dpi=_STATE_TOKEN_DPI, clip=_cover_header_clip(page), alpha=False).tobytes("png")
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return ""
    height, width = image.shape[:2]
    heading = image[
        int(height * 0.45):int(height * 0.88),
        int(width * 0.48):int(width * 0.60),
    ]
    # Scale up to a readable height, never down, instead of a fixed 4x. A
    # fixed multiplier assumed a small crop; once _cover_header_clip became
    # proportional this crop is already ~440px tall on a 2806pt page, and 4x
    # made it 28 megapixels. Tesseract returned '' at --psm 7 on that and
    # nonsense at --psm 6, from a crop that plainly reads "2026 S28".
    scale = max(1, -(-TESSERACT_MIN_HEIGHT_PX // max(1, heading.shape[0])))
    if scale > 1:
        heading = cv2.resize(heading, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(heading, cv2.COLOR_BGR2GRAY)
    _tess_count("header_state_code")
    txt = _tess_string(Image.fromarray(gray), lang="eng", config="--oem 3 --psm 7")
    # Tesseract renders the S as '$' about as often as it gets it right.
    match = re.search(r"([S$U])\s*(\d{2})", txt.upper())
    if not match:
        return ""
    return ("S" if match.group(1) == "$" else match.group(1)) + match.group(2)


def _read_cover_state_name_text(page: fitz.Page) -> str:
    """OCR the page-1 heading in Hindi, where the state is printed by name."""
    image_bytes = getattr(page, "get_pixmap")(
        dpi=_STATE_TOKEN_DPI, clip=_cover_header_clip(page), alpha=False).tobytes("png")
    lines, _confidence = _extract_text_with_tesseract(image_bytes)
    return "\n".join(lines or [])


def _extract_state_code(page: fitz.Page) -> tuple[str, str, str, str]:
    """Resolve the roll's state from page 1.

    Returns ``(state_code, state_name, source, review_reason)``. The printed
    token is the primary read, accepted only when it is a real ECI code. The
    Hindi state name beside it is read only when the token is not usable,
    because that read costs a full-width Tesseract pass on every unit.
    """
    try:
        token = _read_state_token(page)
    except Exception as exc:
        log.warning("State-code token read failed: %s", exc)
        token = ""

    states, catalogue_reason = _active_eci_states()
    if not states:
        if token:
            return token[1:], "", "printed_token_unverified", ""
        return "", "", "", catalogue_reason or "state_code_unreadable"

    token_state = {state["state_cd"]: state for state in states}.get(token)
    if token_state:
        return token_state["code"], token_state["english"], "printed_token", ""

    try:
        name_state = _state_from_text(_read_cover_state_name_text(page), states)
    except Exception as exc:
        log.warning("State-name read failed: %s", exc)
        name_state = None
    if name_state:
        return name_state["code"], name_state["english"], "cover_name_eci", ""
    if token:
        return "", "", "", "state_token_not_in_eci_catalogue"
    return "", "", "", "state_code_unreadable"


def _split_anubhag_label(text: str) -> str:
    """Drop the printed label, returning only what follows it.

    The header reads ``अनुभाग संख्या और नाम : <number>-<locality>``, and
    Tesseract renders that colon as ';' about as often as ':'. Splitting only
    on ':' left the digits behind the label on every ';' line, so no match in
    `_anubhag_code_from_header_text` could fire.

    Both separators are honoured, earliest occurrence wins -- so a line
    containing both splits exactly where it did under ':'-only. A line with
    neither is returned unchanged, which is what an already-bare value needs.
    """
    label_end = min(
        (i for i in (text.find(":"), text.find(";")) if i != -1),
        default=-1,
    )
    return text[label_end + 1:] if label_end != -1 else text


def _anubhag_code_from_header_text(text: str) -> str:
    """Read the leading section number from the scanned roll's header text.

    The field is printed as ``<number>-<locality>``. Tesseract reads the
    narrow leading ``1`` as a danda, ``है`` or ``!`` on some rolls, so those
    variants are accepted only in this leading-number position.

    Two readings this accounts for, both captured verbatim from the 2026 SIR
    rolls:

    * ``।-प्रगति विहार`` (HIN-45) -- U+0964 single danda. The alias class
      previously listed U+0965 double danda only, so this reading parsed to ""
      while the visually identical ``॥-सरस्वती`` (HIN-403) parsed to "1".
    * ``अनुभाग संख्या और नाम; 4-गढी`` (HIN-116) -- the label's ";" where a
      ":" was expected. Splitting only on ":" left the digits behind the label,
      so no match could fire.

    Neither widening invents a value: the match stays anchored to the start of
    the string, and still requires the ``<number>-<locality>`` shape. A header
    that prints no digit (HIN-128) parses to "" from both.
    """
    normalized = text.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
    match = re.match(r"\s*(\d+)\s*[-–—]", normalized)
    if match:
        return match.group(1)
    # A narrow printed ``1`` is also frequently segmented as a bracket.  It
    # is accepted only before the header's required dash, never elsewhere.
    if re.match(r"\s*(?:[॥।!|Il\[\]]+|है[!|]*)\s*[-–—]", normalized):
        return "1"
    return ""


def _clean_anubhag_name_candidate(text: str) -> str:
    """Accept only a credible locality from the printed section header.

    This runs before metadata is copied to every voter record.  It removes
    only observed crop-edge artefacts; uncertain or mixed-script reads stay
    blank rather than becoming a corrupt roll-wide value.
    """
    value = _split_anubhag_label(" ".join(str(text or "").split()))
    value = unicodedata.normalize("NFC", value).translate(
        str.maketrans("०१२३४५६७८९", "0123456789"))
    value = re.sub(r"^\s*(?:\d+|[॥।!|Il\[\]]+|है[!|]*)\s*[-–—]\s*", "", value)
    value = re.sub(r"^\s*[-–—]+\s*", "", value)
    value = re.split(r"\s*(?:\.{2,}|…|\|)\s*", value, maxsplit=1)[0]
    value = re.sub(r"\s+[हह]\s*\d+\s*$", "", value)
    value = re.sub(r"\s+(?:रे|रें|ह|a|A|I|l)\s*$", "", value)
    # On the cover, a narrow printed terminal ``1`` in a phase suffix is
    # routinely returned as a danda/bracket: ``कृष्णा विहार फेस ।``.  This
    # is deliberately *not* a general danda-to-digit rule: it is accepted
    # only after an explicit phase word, where the punctuation cannot be part
    # of a locality name and the source convention is a one-digit phase.
    value = re.sub(
        r"((?:फेस|फेज़?|फीस))\s*[।॥\[\]]\s*$",
        r"\1 1",
        value,
    )
    if re.search(r"ः\s*$", value):
        return ""
    # The narrow printed trailing ``1`` is frequently read as ``]``.  This
    # is a section-name suffix (for example ``आजाद एन्कलेव-1``), not a box
    # rule, and is normalised only in that exact terminal shape.
    value = re.sub(r"[-–—]\s*\]\s*$", "-1", value)
    # Other right-edge crop artefacts are not a locality suffix.
    value = re.sub(r"[-–—]\s*[\]\[।॥!|]+\s*$", "", value)
    value = re.sub(r"\s+[-–—]+\s*$", "", value)
    # A clipped OCR line can lose only the closing glyph of a visibly paired
    # parenthetical locality (``खीमा कालोनी (बेहटा हाजीपुर``).  Restoring the
    # balancing punctuation changes no words or numbers and is safer than
    # publishing a malformed locality to every voter row.
    if value.count("(") == 1 and value.count(")") == 0:
        value += ")"
    # Parentheses can be part of the printed locality (for example
    # ``खीमा कालोनी (बेहटा हाजीपुर)``), not merely decoration around the
    # whole field.  Do not strip a verified closing parenthesis here.
    value = re.sub(r"\u094d(?=[\u093e-\u094c])", "", value).strip(" .:;|[]{}<>")
    if not value or value.endswith(("-", "–", "—", "।", "॥")):
        return ""
    if (len(re.findall(r"[\u0900-\u097F]", value)) < 3
            or re.search(r"[A-Za-z]", value)
            or not re.match(r"^[\u0900-\u097F]", value)):
        return ""
    if re.search(r"\s+\d+\s*$", value) and not re.search(
        r"(?:नंबर|नं|से|सेक्टर|ब्लॉक|गली|फेस|फेज़?|फीस)\s*\d+\s*$", value):
        return ""
    return value


def _restore_cover_subdivision_one(hindi_line: str, english_line: str) -> str:
    """Repair only a cover-list ``-]`` glyph that English independently read as ``-1``.

    A bracket is not generally a digit.  On some cover lists it occurs in the
    middle of a locality (for example ``रेखा गार्डन-] इलायचीपुर``), so the
    terminal-suffix rule above intentionally cannot repair it.  The paired
    English read is a digit-only witness for this exact position; without it
    we retain the original text and let the conservative candidate cleaner
    reject it.
    """
    if not re.search(r"[-–—]\s*[\[\]](?=\s)", hindi_line):
        return hindi_line
    if not re.search(r"[-–—]\s*1(?:\s|$)", english_line):
        return hindi_line
    return re.sub(r"([-–—])\s*[\[\]](?=\s)", r"\g<1>1", hindi_line, count=1)


def _select_anubhag_name(*reads: str) -> tuple[str, str]:
    """Require independent image/segmentation reads to choose one name."""
    candidates = [_clean_anubhag_name_candidate(read) for read in reads]
    counts = Counter(candidate for candidate in candidates if candidate)
    if not counts:
        return "", "anubhag_name_unreadable_from_printed_header"
    ranked = counts.most_common()
    name, votes = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    return (name, "") if votes >= 2 and votes > runner_up else (
        "", "anubhag_name_conflicting_header_reads")


def _extract_roll_header_metadata(page: fitz.Page) -> dict[str, str]:
    """Extract roll-wide codes (ac, anubhag, booth) and Hindi section name from page 3 header. Returns empty dict on failure."""
    empty = {
        "state_code": "",
        "ac_code": "",
        "anubhag_code": "",
        "anubhag_name": "",
        "anubhag_status": "applicable",
        "anubhag_review_reason": "",
        "booth_code": "",
    }
    try:
        # Include the full baseline of the second header line.  A 40-point
        # clip cut descenders on some scans (HIN-138/HIN-265) and made the
        # otherwise readable labelled line disappear entirely.
        header_height = min(52.0, page.rect.height)
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
            _tess_count("header_number")
            t = _tess_string(Image.fromarray(gray), lang="eng", config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789").strip()
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
        if not ac_code:
            ac_code = _extract_ac_code_from_header_fallback(page)

        anubhag_region = crop(0.016, 0.33, 0.363, 0.68)
        anubhag_gray = cv2.cvtColor(anubhag_region, cv2.COLOR_BGR2GRAY)
        # The code crop stays narrow, but a locality name must not be clipped.
        # The label and ``<section>-`` prefix occupy the far-left header
        # band.  Giving that noise to the name recogniser made it consume the
        # first locality word (for example, ``गढ़ी`` became ``778``).  Start
        # immediately after the structural prefix; code extraction above
        # remains independent and retains the full narrow crop.
        anubhag_name_region = crop(0.120, 0.33, 0.500, 0.68, scale=1.8)
        anubhag_name_gray = cv2.cvtColor(anubhag_name_region, cv2.COLOR_BGR2GRAY)

        def anubhag_line(gray: Any, psm: int) -> str:
            text = pytesseract.image_to_string(
                Image.fromarray(gray), lang="hin",
                config=f"--oem 3 --psm {psm}")
            text = re.sub(r"[\u200b-\u200f\u00ad]", "", text)
            return _split_anubhag_label(" ".join(text.split()))

        def anubhag_code_line(gray: Any, psm: int) -> str:
            # Keep numeric recognition independent from Hindi locality OCR.
            # The whitelist prevents Hindi label text from contributing a
            # number; the parser below still requires a leading ``number-``.
            return pytesseract.image_to_string(
                Image.fromarray(gray), lang="eng",
                config=(f"--oem 3 --psm {psm} "
                        "-c tessedit_char_whitelist=0123456789-[]|!"),
            ).strip()

        # psm 7 treats the header band as one line and mis-reads the narrow
        # leading digit on some rolls -- 'jae ea 7' where psm 6 reads
        # '॥-लक्ष्मी इन्कलेव 7' (HIN-120). psm 6 keeps the same text but a
        # different block segmentation, so it recovers those. This is the same
        # shape as `header_number` above, which already retries ac_code and
        # booth_code; anubhag was the one header field with no second read.
        #
        # Only a code that parses is accepted. If neither read yields one the
        # field stays blank -- a header that prints no digit (HIN-128) must not
        # be given a value by a fallback.
        # The broad header crop can blend the leading section glyph with
        # adjacent text and return values such as ``14``/``113``.  Numeric
        # metadata therefore comes exclusively from the isolated,
        # English-only high-resolution glyph reader below.
        anubhag_text = ""
        anubhag_code = _extract_anubhag_code_from_header_fallback(page)
        # Two layout segmentations plus two contrast treatments give us four
        # independent reads of the exact same printed locality line.
        _threshold, otsu = cv2.threshold(
            anubhag_name_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            anubhag_name_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 9)
        name_reads = [
            anubhag_line(gray, psm)
            for gray in (anubhag_name_gray, otsu, adaptive)
            # PSM 13 is a raw single-line recogniser.  It is useful for
            # small printed headers where PSM 6/7 mistakenly treats the
            # leading locality word as part of the section-number prefix.
            for psm in (7, 6, 13)
        ]
        cleaned_name_reads = [_clean_anubhag_name_candidate(read) for read in name_reads]
        anubhag_name, anubhag_name_reason = _select_anubhag_name(*name_reads)

        return {
            "state_code": "",
            "ac_code": ac_code,
            "anubhag_code": anubhag_code,
            "anubhag_name": anubhag_name,
            # Internal diagnostic evidence.  The response adapter copies only
            # declared roll fields, so this never becomes voter data.
            "anubhag_name_candidates": cleaned_name_reads,
            "anubhag_name_raw_reads": name_reads,
            "anubhag_status": "applicable",
            "anubhag_review_reason": anubhag_name_reason,
            "booth_code": booth_code,
        }
    except Exception as exc:
        log.warning("Roll header metadata extraction failed: %s", exc)
        return empty


def _extract_booth_code_from_cover(page: fitz.Page) -> str:
    """Read the cover's explicitly labelled part number, if it has one.

    Most scanned rolls repeat the metadata strip on page 3, which is why the
    main header reader uses that page.  Some valid rolls instead print the
    part number only on the cover: ``भाग संख्या : 8``.  Do not substitute an
    unlabelled number from the cover; that could be an AC, year, voter total,
    or polling-station number.  This fallback accepts only the value following
    the printed Hindi/English part-number label.
    """
    try:
        # Proportional for the same reason as _cover_header_clip: the part
        # number is printed in the same top band as the state token, and a
        # fixed 130pt reaches 15% of an 842pt page but only 4.6% of a 2806pt
        # one. 9% was still short -- it clipped the label's bottom row off and
        # found no "भाग संख्या" at all; 12% reads it in full. 20% also works and
        # is what the 842pt page always had, so take the widest band that stays
        # cheap: 20% of the cover is 20% of a page, not of the document.
        cover_height = max(130.0, page.rect.height * 0.20)
        cover_clip = fitz.Rect(
            page.rect.x0, page.rect.y0,
            page.rect.x1, page.rect.y0 + cover_height,
        )
        image_bytes = page.get_pixmap(
            dpi=300, clip=cover_clip, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return ""
        text = pytesseract.image_to_string(
            Image.fromarray(image), lang="hin+eng", config="--oem 3 --psm 6")
        text = text.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        match = re.search(
            r"(?:भाग\s*संख्या|part\s*(?:number|no\.?))(?:\s*[:：;,.])*\s*(\d+)",
            text,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""
    except Exception as exc:
        log.warning("Cover booth-code extraction failed: %s", exc)
        return ""


def _extract_anubhag_entries_from_cover(page: fitz.Page) -> dict[str, str]:
    """Extract the authoritative section-code/name list printed on page 1.

    The cover has a dedicated block headed ``भाग में अनुभागों की संख्या और
    नाम``.  Unlike the tiny repeated voter-page header, this is the roll's
    master list and may contain more than one section.  Only numbered lines
    inside that labelled block are accepted; unrelated cover text can never
    become section metadata.
    """
    try:
        width, height = page.rect.width, page.rect.height
        # At 150 DPI the cover's larger print is materially cleaner than an
        # upscaled 300-DPI read (measured on the review set: एन्कलेव/ओक्सी
        # remain intact).  Keep the crop inside the left-hand section-list
        # column so village/ward labels cannot leak into the locality.
        # A cover can list five or more sections.  The old 0.40 cut ended in
        # the middle of entries 4/5, so later voter pages could correctly read
        # their number but had no master-name entry to copy.  0.46 includes
        # the complete list on the known layouts while the explicit labels
        # below still prevent village/ward fields underneath from being used.
        clip = fitz.Rect(0, height * 0.27, width * 0.46, height * 0.46)
        image_bytes = page.get_pixmap(
            dpi=150, clip=clip, alpha=False
        ).tobytes("png")
        image = cv2.imdecode(
            np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR
        )
        if image is None or image.size == 0:
            return {}
        hindi_text = pytesseract.image_to_string(
            Image.fromarray(image), lang="hin", config="--oem 3 --psm 11"
        )
        # The independent English pass owns digits only.  Besides reliably
        # separating the leading 1/4 shape, it recovers embedded address
        # ranges such as ``म0न0 114 से 226`` that Hindi OCR reads as ``4``.
        english_text = pytesseract.image_to_string(
            Image.fromarray(image), lang="eng", config="--oem 3 --psm 11"
        )
        lines = [
            " ".join(line.split())
            for line in hindi_text.splitlines()
            if line.strip()
        ]
        label_index = next(
            (
                index
                for index, line in enumerate(lines)
                if "अनुभाग" in line and "संख्या" in line and "नाम" in line
            ),
            None,
        )
        if label_index is None:
            return {}

        entry_line = re.compile(
            r"\s*(?:\d+|[०-९]+|[॥।!|Il\[\]]+)?\s*[-–—]"
        )
        hindi_entry_lines: list[str] = []
        for line in lines[label_index + 1:]:
            # A numbered locality is a section entry even when the locality
            # contains a cover-field word. ``ए ब्लॉक`` was being thrown away
            # because the bare label ``ब्लॉक`` is also a field on this form,
            # and the next entry was then saved under the wrong number.
            if entry_line.match(line):
                hindi_entry_lines.append(line)
                continue
            # The next labelled cover field marks the end of the left-hand
            # section list.  This also prevents village/ward text from being
            # interpreted as another entry.
            if any(label in line for label in (
                "कस्बा अथवा ग्राम", "वार्ड", "डाकघर", "थाना", "पंचायत",
                "ब्लॉक", "तहसील", "जिला", "पिन कोड",
            )):
                continue

        english_entry_lines = [
            " ".join(line.split())
            for line in english_text.splitlines()
            if re.match(r"\s*\d+\s*(?:[-–—]|\s)", line)
        ]

        entries: dict[str, str] = {}
        for index, line in enumerate(hindi_entry_lines):
            english_line = (
                english_entry_lines[index]
                if index < len(english_entry_lines)
                else ""
            )
            english_code_match = re.match(r"\s*(\d+)\s*(?:[-–—]|\s)", english_line)
            english_code = english_code_match.group(1) if english_code_match else ""
            hindi_code = _anubhag_code_from_header_text(line)
            expected_code = str(index + 1)
            # This cover block is an enumeration, not an arbitrary code list:
            # it begins at 1 and increments by one.  Validate OCR against that
            # printed row order.  The first narrow 1 is routinely read as a
            # dash; later disagreement means a row was missed and is rejected
            # rather than silently renumbered.  The digit on the Hindi line
            # wins when it is present.  An English digit from a different row
            # must not rename section 4 as section 3.
            if index == 0:
                code = "1"
            elif hindi_code:
                if hindi_code != expected_code:
                    continue
                code = hindi_code
            elif english_code == expected_code:
                code = expected_code
            else:
                continue
            # Hindi OCR sometimes retains the suffix dash but loses its
            # narrow digit.  Restore it only when the independent English
            # rendering sees an explicit terminal ``-<digits>``.
            english_suffix = re.search(r"[-–—]\s*(\d+)\s*$", english_line)
            if (english_suffix and len(english_suffix.group(1)) == 1
                    and re.search(r"[-–—]\s*$", line)):
                line = line + english_suffix.group(1)
            line = _restore_cover_subdivision_one(line, english_line)
            # Some covers print a one-digit locality subdivision with a space
            # instead of a dash (``जाफराबाद गनौली १``).  Preserve the value in
            # the API's normal ASCII suffix form; longer address numbers are
            # deliberately not rewritten by this rule.
            line = re.sub(r"\s+([०-९\d])\s*$", r"-\1", line)
            name = _clean_anubhag_name_candidate(line)
            if name and english_line:
                english_body = re.sub(
                    r"^\s*\d+\s*[-–—]\s*", "", english_line
                )
                english_numbers = re.findall(r"\d+", english_body)
                if english_numbers and re.search(r"(?:म0न0|\bसे\b)", name):
                    matches = list(re.finditer(r"\d+", name))
                    # Align from the right: Hindi abbreviations such as
                    # ``म0न0`` legitimately contain zeros which the English
                    # transliteration may omit, while the trailing address
                    # range (114, 226) is shared by both reads.
                    replacements = list(zip(
                        matches[-len(english_numbers):], english_numbers
                    ))
                    for match, replacement in reversed(replacements):
                        name = name[:match.start()] + replacement + name[match.end():]
            if not code or not name:
                continue
            previous = entries.get(code)
            if previous and previous != name:
                # A duplicated code with conflicting text is not safe enough
                # to copy to every record.  Drop it instead of guessing.
                entries.pop(code, None)
                continue
            entries[code] = name
        return entries
    except Exception as exc:
        log.warning("Cover anubhag-list extraction failed: %s", exc)
        return {}


# The digit run that ends an anubhag code, just before the name's hyphen, as in
# the '4-' of '4-एकत्ता विहार'.  The leading group is non-digits so the stray
# digit PaddleOCR renders from the Devanagari label is not swallowed here --
# the caller arbitrates the run against the cover list instead.
ANUBHAG_CODE_RUN_RE = re.compile(r"(?:^|\D)(\d{1,3})\s*[-–—]")


def _page_prints_section_header(page: fitz.Page) -> bool:
    """True when the page prints an ``अनुभाग संख्या और नाम`` line at all.

    Separates a supplement page -- which prints the part number and a date
    range but no section line -- from a page whose section we failed to read.
    The two failures look identical from the code alone: both leave the code
    blank, but only one of them is a reading failure worth reviewing.
    """
    try:
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + min(52.0, page.rect.height))
        image = cv2.imdecode(np.frombuffer(page.get_pixmap(dpi=150, clip=clip, alpha=False).tobytes("png"), np.uint8), cv2.IMREAD_COLOR)
        if image is None: return False
        text = pytesseract.image_to_string(
            Image.fromarray(image), lang="hin", config="--oem 3 --psm 6",
        )
        return "अनुभाग" in text
    except Exception as exc:
        log.warning("Section-header presence probe failed: %s", exc)
        return False


def _supplement_list_title(page: fitz.Page) -> str:
    """The addition-list title printed where a section name would be.

    These pages have no अनुभाग. The header says ``परिवर्धन की सूची 1`` (or
    2, 3, …). That title is the value to show, rather than a blank section.
    """
    try:
        clip = fitz.Rect(
            page.rect.x0, page.rect.y0, page.rect.x1,
            page.rect.y0 + min(70.0, page.rect.height),
        )
        image = cv2.imdecode(
            np.frombuffer(
                page.get_pixmap(dpi=160, clip=clip, alpha=False).tobytes("png"),
                np.uint8,
            ),
            cv2.IMREAD_COLOR,
        )
        if image is None:
            return ""
        text = pytesseract.image_to_string(
            Image.fromarray(image), lang="hin", config="--oem 3 --psm 6",
        )
        match = re.search(r"परिवर्धन\s*की\s*सूची\s*(\d+)?", text)
        if not match:
            return ""
        number = match.group(1)
        return "परिवर्धन की सूची" + (f" {number}" if number else "")
    except Exception as exc:
        log.warning("Supplement-title read failed: %s", exc)
        return ""


def _anubhag_digit_runs_via_paddle(page: fitz.Page) -> list[str]:
    """Return every digit run PaddleOCR reads off the printed section header.

    Paddle is not a replacement for the narrow crop: it is a *line* reader, so
    a bare digit glyph crop has no text region for it to detect and it returns
    nothing at all.  Fed the header strip, it reads the ``code-name`` token that
    follows the label colon reliably -- including the numeral 1, which
    Tesseract's Hindi model renders as 4.

    Runs are returned unordered and unfiltered; the caller arbitrates them
    against the cover's own section list, because Paddle sometimes prefixes the
    run with a stray digit from the Devanagari label (``43-`` for a real 3).
    """
    runs: list[str] = []
    if PaddleOCR is None:
        return runs
    try:
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + min(52.0, page.rect.height))
        image = cv2.imdecode(np.frombuffer(page.get_pixmap(dpi=1200, clip=clip, alpha=False).tobytes("png"), np.uint8), cv2.IMREAD_COLOR)
        if image is None: return runs
        h = image.shape[0]
        for y0, y1 in ((.28, .52), (.00, 1.00)):
            region = image[int(h*y0):int(h*y1), :]
            try:
                result = _get_paddle().ocr(region, cls=False)
            except Exception as exc:
                log.debug("Paddle anubhag digit read failed: %s", exc)
                continue
            for line in (result[0] or []):
                if not line or len(line) < 2 or not line[1]:
                    continue
                match = ANUBHAG_CODE_RUN_RE.search(line[1][0])
                if match:
                    runs.append(match.group(1))
    except Exception as exc:
        log.warning("Paddle anubhag digit read failed: %s", exc)
    return runs


def _arbitrate_anubhag_digit_runs(runs: list[str], entries: dict[str, str]) -> str:
    """Let the cover's section list decide which Paddle run is the real code.

    A run is accepted only if it is an actual section number in ``entries``.
    Paddle prefixes the label's own stray digit to runs whose code is 2 or 3
    (``43-`` for a real 3), so when the whole run is not a known section, its
    last digit is tried -- which keeps a genuine two-digit section number
    intact, because that matches directly and is never truncated.
    """
    for run in runs:
        if run.strip() in entries:
            return run.strip()
    for run in runs:
        if run.strip()[-1:] in entries:
            return run.strip()[-1:]
    return ""


def _extract_anubhag_code_from_header_fallback(
    page: fitz.Page, entries: Optional[dict[str, str]] = None,
) -> str:
    """Recover a section code from the complete printed header line.

    The original narrow crop is deliberately conservative, but some layouts
    place ``अनुभाग संख्या और नाम : 1-…`` just below it.  OCR the complete
    top strip and still require the explicit section label and a verified
    leading-number form.  This is a fallback for a missed read, never a
    default section number.

    Without ``entries`` this is the original narrow English-only crop, which
    is high precision and low recall: it reads a code when it can prove one,
    and returns blank otherwise.  Callers that hold the cover's section list
    should pass it, which additionally lets a blank read be recovered.
    """
    # The old fallback used a combined Hindi+English pass to find the label
    # and then trusted the same pass for the digit.  That is precisely the
    # 1/4 confusion this function must not amplify.  The primary narrow crop
    # now owns numeric extraction through English-only digit OCR; when that
    # cannot prove a code, return blank rather than borrowing an unlabelled
    # number from the rest of the header.
    try:
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y0 + min(52.0, page.rect.height))
        image = cv2.imdecode(np.frombuffer(page.get_pixmap(dpi=1200, clip=clip, alpha=False).tobytes("png"), np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            h, w = image.shape[:2]
            candidates: set[str] = set()
            for x0, x1 in ((.155, .205), (.16, .22), (.16, .24)):
                glyph = image[int(h*.30):int(h*.50), int(w*x0):int(w*x1)]
                gray = cv2.cvtColor(glyph, cv2.COLOR_BGR2GRAY)
                _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                binary = cv2.resize(binary, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
                value = pytesseract.image_to_string(
                    Image.fromarray(binary), lang="eng",
                    config="--oem 3 --psm 13 -c tessedit_char_whitelist=0123456789",
                ).strip()
                # Reject multi-digit output: it is neighbouring-text bleed,
                # measured as 14/113/174 on the validation batch.
                if len(value) == 1 and value.isdigit():
                    candidates.add(value)
            if len(candidates) == 1:
                return next(iter(candidates))
    except Exception as exc:
        # Fall through rather than return: a failed render must not stop the
        # cover-arbitrated recovery below, which is an independent reader.
        log.warning("English anubhag digit fallback failed: %s", exc)

    # The narrow crop could not prove a code.  Only now, and only against the
    # cover's own list, is a second reader allowed to speak: Tesseract's Hindi
    # model reads the printed numeral 1 as 4 on this roll, so its digit is
    # unusable, but PaddleOCR reads the same pixels correctly.  The cover
    # arbitrates the result, so a wrong read has no section name to match.
    #
    # The page must first be shown to print a section line at all.  A
    # supplement page carries no such line, yet Paddle still finds digit-hyphen
    # runs elsewhere on it, and the cover happily vouches for a run that was
    # never a section code.  Without this guard HIN-102 p26 -- a supplement --
    # was assigned section 3.
    if entries and _page_prints_section_header(page):
        recovered = _arbitrate_anubhag_digit_runs(
            _anubhag_digit_runs_via_paddle(page), entries
        )
        if recovered:
            log.info("Recovered anubhag_code=%s via Paddle + cover arbitration", recovered)
        return recovered
    return ""


def _resolve_anubhag_code_from_repeated_headers(
    document: fitz.Document, metadata_page_number: int, initial_code: str,
) -> tuple[str, str]:
    """Resolve a scanned-roll section number without inventing one.

    A roll-wide section header is reprinted on every voter-card page.  A
    single narrow scan can lose its leading digit, so a blank first read is
    retried on a small, spread-out sample of later card pages.  We accept a
    recovered value only when at least two independently rendered page headers
    agree and no page produces a different numeric value.  A repeated OCR
    mistake must therefore not silently become metadata for an entire PDF.

    The returned reason is deliberately machine-readable.  Callers propagate
    it to every card as a review reason, rather than leaving an apparently
    healthy record with missing roll metadata.
    """
    if initial_code:
        return initial_code, ""

    # Page 3 is the normal metadata page.  The offsets sample adjacent and
    # later voter pages without adding a full-document OCR pass to every PDF.
    # (Indexes are zero-based; metadata_page_number is one-based.)
    candidate_indexes = [
        metadata_page_number - 1,
        metadata_page_number,
        metadata_page_number + 1,
        metadata_page_number + 3,
        metadata_page_number + 7,
    ]
    readings: list[str] = []
    seen_indexes: set[int] = set()
    for index in candidate_indexes:
        if index < 0 or index >= len(document) or index in seen_indexes:
            continue
        seen_indexes.add(index)
        code = _extract_anubhag_code_from_header_fallback(document[index])
        if code:
            readings.append(code)

    distinct = set(readings)
    if len(readings) >= 2 and len(distinct) == 1:
        code = readings[0]
        log.info(
            "Recovered anubhag_code=%s from %d agreeing repeated headers",
            code, len(readings),
        )
        return code, ""
    if distinct:
        log.warning(
            "Anubhag code unresolved: conflicting repeated-header readings %s",
            sorted(distinct),
        )
        return "", "anubhag_code_conflicting_header_reads"
    log.warning("Anubhag code unresolved: no verified read in repeated headers")
    return "", "anubhag_code_unreadable_from_printed_header"


def _extract_anubhag_name_from_header_fallback(page: fitz.Page) -> str:
    """Read the complete labelled Hindi header line as a name-only fallback.

    Some scans have a very clear printed ``अनुभाग संख्या और नाम`` line, but
    their tight locality crop is degraded by a rule or loses its first word.
    Read a wider, higher-resolution strip and accept only the line explicitly
    carrying that label.  The parser still removes the leading section number
    and applies the same strict Hindi cleaner; this cannot source text from a
    voter card below the header.
    """
    try:
        # Keep this identical to the clean diagnostic/header rendering path.
        # Upscaling this strip to 600 DPI and cropping it a second time was
        # measured to turn clear text such as ``अमित विहार-1`` into repeated
        # garbage (``॥ल्अमितविहारनज``).  At 300 DPI the complete labelled
        # line is preserved and Tesseract's page segmentation has enough
        # context to recognise it correctly.
        header_height = min(40.0, page.rect.height)
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                         page.rect.y0 + header_height)
        image_bytes = page.get_pixmap(dpi=300, clip=clip, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return ""
        # OCR the complete header, not a percentage crop.  Acceptance still
        # requires the explicit printed label below, so card text cannot be
        # promoted to roll metadata.
        text = pytesseract.image_to_string(
            Image.fromarray(image), lang="hin", config="--oem 3 --psm 6")
        for line in text.splitlines():
            if "अनुभाग" not in line or "नाम" not in line:
                continue
            return _clean_anubhag_name_candidate(_split_anubhag_label(line))
        return ""
    except Exception as exc:
        log.warning("Header anubhag-name fallback failed: %s", exc)
        return ""


def _resolve_anubhag_name_from_repeated_headers(
    document: fitz.Document, metadata_page_number: int, initial_name: str,
) -> tuple[str, str]:
    """Recover a section name only when repeated printed headers agree.

    The section number and locality are independent fields.  A faint leading
    digit may be ambiguous while the Hindi locality is plainly legible on
    every card page.  Previously that ambiguity caused the API to discard a
    useful name along with the uncertain number, and an older permissive read
    could leave a corrupt name copied to the entire document.

    A single page still needs the strict intra-image consensus in
    ``_extract_roll_header_metadata``.  If that leaves the name blank, this
    fallback accepts a value only where three or more *separate pages* expose
    exactly one credible candidate and every observed candidate agrees.  The
    number is intentionally not consulted or inferred here.
    """
    if initial_name:
        suffix = initial_name.rsplit("-", 1)[-1] if "-" in initial_name else ""
        # OCR bleed such as ``आजाद एन्कलेव-व ब`` or ``...-॥ ब`` is not a
        # plausible locality suffix.  Let the complete labelled-line reader
        # retry it instead of treating a merely non-empty string as final.
        if ((suffix and len(suffix) <= 4 and " " in suffix)
                or re.search(r"[॥।!|\[\]]", initial_name)):
            initial_name = ""

    if initial_name:
        # The tight crop is normally the cleanest spelling, but its right edge
        # can omit a printed ``-1``/``-2`` suffix.  Enrich it only when the
        # complete explicitly-labelled line contains the *exact* same base
        # followed solely by a numeric suffix.  A conflicting OCR spelling
        # must never replace the tight read.
        page_index = metadata_page_number - 1
        if 0 <= page_index < len(document):
            complete = _extract_anubhag_name_from_header_fallback(
                document[page_index]
            )
            if re.fullmatch(re.escape(initial_name) + r"\s*-\s*\d+", complete):
                return complete, ""
        return initial_name, ""

    candidate_indexes = [
        metadata_page_number - 1,
        metadata_page_number,
        metadata_page_number + 1,
        metadata_page_number + 2,
        metadata_page_number + 3,
    ]
    readings: list[str] = []
    seen_indexes: set[int] = set()
    for index in candidate_indexes:
        if index < 0 or index >= len(document) or index in seen_indexes:
            continue
        seen_indexes.add(index)
        metadata = _extract_roll_header_metadata(document[index])
        # Once the tight crop failed to establish a name, prefer the explicit
        # labelled full-header line.  The tight variants can contain a lone
        # but incomplete word; letting it compete with the labelled read
        # caused a visibly clear header (for example ``आजाद एन्कलेव``) to be
        # discarded as a conflict.  The fallback is still constrained to the
        # printed label and is independently repeated across pages below.
        recovered = ""
        if not metadata.get("anubhag_name"):
            recovered = _extract_anubhag_name_from_header_fallback(document[index])
        if recovered:
            page_values = {recovered}
        else:
            # Each individual variant has already passed the conservative Hindi
            # cleaning rule.  A page contributes only when its non-empty variants
            # agree; this prevents one noisy PSM read from becoming roll metadata.
            page_values = {
                value for value in metadata.get("anubhag_name_candidates", []) if value
            }
        if len(page_values) == 1:
            readings.append(next(iter(page_values)))

    distinct = set(readings)
    if len(readings) >= 3 and len(distinct) == 1:
        name = readings[0]
        log.info(
            "Recovered anubhag_name=%r from %d agreeing repeated headers",
            name, len(readings),
        )
        return name, ""
    if distinct:
        log.warning(
            "Anubhag name unresolved: conflicting repeated-header readings %s",
            sorted(distinct),
        )
        return "", "anubhag_name_conflicting_header_reads"
    log.warning("Anubhag name unresolved: no verified read in repeated headers")
    return "", "anubhag_name_unreadable_from_printed_header"


def _combine_anubhag_review_reasons(*reasons: str) -> str:
    """Keep independent number/name warnings rather than overwriting one."""
    return ";".join(dict.fromkeys(reason for reason in reasons if reason))


def _extract_ac_code_from_header_fallback(page: fitz.Page) -> str:
    """Read an AC number only from its explicitly labelled header line."""
    try:
        header_height = min(40.0, page.rect.height)
        header_clip = fitz.Rect(
            page.rect.x0, page.rect.y0,
            page.rect.x1, page.rect.y0 + header_height,
        )
        image_bytes = page.get_pixmap(
            dpi=300, clip=header_clip, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return ""
        text = pytesseract.image_to_string(
            Image.fromarray(image), lang="hin+eng", config="--oem 3 --psm 6")
        text = text.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        # The AC header is printed as "विधानसभा निर्वाचन क्षेत्र : 53-लोनी".
        # The label is required, so no year, booth, or voter-count number can
        # become an AC code by accident.
        match = re.search(
            r"विधानसभा\s*निर्वाचन\s*क्षेत्र[^:\n;：]*[:;：]\s*(\d+)\s*[-–—]",
            text,
        )
        return match.group(1) if match else ""
    except Exception as exc:
        log.warning("Header AC-code fallback failed: %s", exc)
        return ""

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

        # Reset the card counter now that the page range is known. Done before
        # the first page so a poller arriving mid-run never sees a count left
        # over from the previous request.
        _progress_reset(pages_total=max(0, last - first + 1))
        # Same reasoning for the launch counters: reset here so each request
        # reports its own launches rather than a running total since import.
        _tess_counters_reset()
        _tess_shadow_reset()

        metadata_page_number = 3 if page_count >= 3 else 1
        metadata_started = time.perf_counter()
        roll_metadata = _cached_roll_metadata(pdf_bytes)
        if roll_metadata is None:
            roll_metadata = _extract_roll_header_metadata(document[metadata_page_number - 1])
            initial_anubhag_reason = roll_metadata.get("anubhag_review_reason", "")
            (
                roll_metadata["anubhag_code"],
                code_review_reason,
            ) = _resolve_anubhag_code_from_repeated_headers(
                document, metadata_page_number, roll_metadata["anubhag_code"])
            (
                roll_metadata["anubhag_name"],
                name_review_reason,
            ) = _resolve_anubhag_name_from_repeated_headers(
                document, metadata_page_number, roll_metadata["anubhag_name"])
            roll_metadata["anubhag_review_reason"] = _combine_anubhag_review_reasons(
                "" if roll_metadata["anubhag_name"] else initial_anubhag_reason,
                code_review_reason, name_review_reason)
            if not roll_metadata["booth_code"]:
                roll_metadata["booth_code"] = _extract_booth_code_from_cover(document[0])
            (
                roll_metadata["state_code"],
                roll_metadata["state_name"],
                roll_metadata["state_source"],
                roll_metadata["state_review_reason"],
            ) = _extract_state_code(document[0])
            cover_anubhag_entries = _extract_anubhag_entries_from_cover(document[0])
            if cover_anubhag_entries:
                roll_metadata["anubhag_entries"] = dict(cover_anubhag_entries)
                if len(cover_anubhag_entries) == 1:
                    cover_code, cover_name = next(iter(cover_anubhag_entries.items()))
                    roll_metadata["anubhag_code"] = cover_code
                    roll_metadata["anubhag_name"] = cover_name
                    roll_metadata["anubhag_review_reason"] = ""
                else:
                    roll_metadata["anubhag_code"] = ""
                    roll_metadata["anubhag_name"] = ""
                    roll_metadata["anubhag_status"] = "per_page"
                    roll_metadata["anubhag_review_reason"] = ""
            # A header read is document metadata, not a defect in every voter
            # card below it.  Make that distinction explicit for consumers of
            # the API/UI: a blank name with a known section code means the
            # printed header needs verification, not that thousands of voters
            # have missing section data.
            if (roll_metadata.get("anubhag_code")
                    and not roll_metadata.get("anubhag_name")):
                roll_metadata["anubhag_status"] = "header_needs_verification"
            _cache_roll_metadata(pdf_bytes, roll_metadata)
            cache_state = "miss"
        else:
            cache_state = "hit"
        cover_anubhag_entries = dict(roll_metadata.get("anubhag_entries") or {})
        print(f"[OCR] header_metadata page={metadata_page_number} cache={cache_state} elapsed={time.perf_counter() - metadata_started:.2f}s", flush=True)

        records: list[dict[str, Any]] = []
        processed_pages: list[dict[str, Any]] = []
        skipped_pages: list[dict[str, Any]] = []
        serial_corrections: list[dict[str, Any]] = []
        # Section number last printed in this part. An addition list does not
        # print a new one, so later pages keep this value.
        last_section_code = roll_metadata.get("anubhag_code") or ""

        for page_number in range(first, last + 1):
            page = document[page_number - 1]
            probe_started = time.perf_counter()
            _progress_set_phase("probe")
            _progress(f"grid probe on {len(_voter_card_rects(page))} card positions", force=True)
            if skip_non_voter_pages:
                looks_like_voter_grid, probe_hits, probe_count = _page_looks_like_voter_grid(page)
            else:
                looks_like_voter_grid, probe_hits, probe_count = True, 0, 0
            probe_elapsed = time.perf_counter() - probe_started
            print(f"[OCR] page={page_number} grid_probe={probe_elapsed:.2f}s voter_grid={looks_like_voter_grid} epic_hits={probe_hits}/{probe_count}", flush=True)
            if not looks_like_voter_grid:
                _progress(f"page {page_number} SKIPPED (not a voter grid)", force=True)
                skipped_pages.append({
                    "page_number": page_number,
                    "reason": (
                        f"only {probe_hits} of {probe_count} probed card positions "
                        f"held an EPIC; {MIN_EPIC_HITS_PER_PAGE} required for a voter grid"
                    ),
                })
                continue

            page_roll_metadata = dict(roll_metadata)
            if len(cover_anubhag_entries) == 1:
                cover_code, cover_name = next(iter(cover_anubhag_entries.items()))
                page_roll_metadata["anubhag_code"] = cover_code
                page_roll_metadata["anubhag_name"] = cover_name
                page_roll_metadata["anubhag_review_reason"] = ""
            elif len(cover_anubhag_entries) > 1:
                page_code = _extract_anubhag_code_from_header_fallback(
                    page, cover_anubhag_entries
                )
                page_name = cover_anubhag_entries.get(page_code, "")
                if page_code and not page_name:
                    # The page printed a section number the cover list did
                    # not keep. Read the labelled line on this page. Clearing
                    # the number lets the neighbour fill copy the previous
                    # section onto this one and leave the name blank.
                    page_name = _extract_anubhag_name_from_header_fallback(page)
                page_roll_metadata["anubhag_status"] = "per_page"
                if page_code and page_name:
                    page_roll_metadata["anubhag_code"] = page_code
                    page_roll_metadata["anubhag_name"] = page_name
                    page_roll_metadata["anubhag_review_reason"] = ""
                elif page_code:
                    page_roll_metadata["anubhag_code"] = page_code
                    page_roll_metadata["anubhag_name"] = ""
                    page_roll_metadata["anubhag_review_reason"] = (
                        "anubhag_cover_entry_unmatched_for_page"
                    )
                elif _page_prints_section_header(page):
                    # The page does print ``अनुभाग संख्या और नाम`` but no
                    # reader could prove which section it is.  A genuine miss.
                    page_roll_metadata["anubhag_review_reason"] = (
                        "anubhag_cover_entry_unmatched_for_page"
                    )
                else:
                    # A supplement ('परिवर्धन की सूची') page prints the part
                    # number and a date range but no section line at all.
                    # The list title is what belongs in the section column.
                    title = _supplement_list_title(page)
                    if title:
                        page_roll_metadata["anubhag_name"] = title
                        # The list does not print a new अनुभाग संख्या. Keep
                        # the number already in force for this part instead
                        # of clearing it.
                        page_roll_metadata["anubhag_code"] = last_section_code
                        page_roll_metadata["anubhag_review_reason"] = ""
                    else:
                        page_roll_metadata["anubhag_review_reason"] = (
                            "supplement_page_no_section_header"
                        )

            carried_code = page_roll_metadata.get("anubhag_code") or ""
            if carried_code:
                last_section_code = carried_code

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
                    # Counted after _extract_card has fully returned, so the
                    # counter cannot influence what it produced. This is the
                    # only success path; the except below re-raises without
                    # counting, which is correct -- a card that raised produced
                    # nothing, and the page-level total still reflects it.
                    _progress_card_done(
                        page_number=page_number, records=1 if rec else 0
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
                # Reused across pages rather than rebuilt per page, so anything
                # cached per thread (Tesseract engines, HTTP pools) survives
                # the page boundary. Safe because _OCR_LOCK admits one
                # extraction at a time, so the pool can never be oversubscribed
                # by concurrent requests.
                executor = _card_pool()
                # Deliberately NOT shutdown() here. shutdown(wait=True) is what
                # the old per-page `with` block did, but it is terminal -- a
                # shut-down executor raises "cannot schedule new futures after
                # shutdown" on its next map, which would break the second page.
                # Reuse across pages is the entire point: a warm thread keeps
                # whatever it has cached (Tesseract engines) instead of paying
                # the cold-start cost once per page per worker.
                #
                # map() returns a lazy iterator, so list() below is what forces
                # the work to finish. If a card raises, list() re-raises once
                # the in-flight siblings have drained, matching the old
                # wait=True behaviour without tearing the pool down.
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
                # Keep the decoded serial crop for the deleted-mark vote. The
                # stacked pass re-scales and re-pads this same crop before
                # reading it, and one read of the stacked image can lose a
                # legible Q (page 16 card 20 came back ['410']).
                ser_img = None
                if ser_png:
                    ser_img = cv2.imdecode(
                        np.frombuffer(ser_png, np.uint8), cv2.IMREAD_COLOR
                    )
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
                field_confidences = {}
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

                        # Extract PaddleOCR confidence scores per field
                        field_confidences = {}
                        for field, (fy0, fy1) in h_offsets.items():
                            field_hits = [
                                ln for ln in hits
                                if fy0 <= sum(pt[1] for pt in ln[0]) / 4 <= fy1
                            ]
                            field_texts = [ln[1][0] for ln in field_hits]
                            field_confs = [ln[1][1] for ln in field_hits]
                            field_confidences[field] = sum(field_confs) / len(field_confs) if field_confs else 0.0

                            if field == "serial":
                                # A single Paddle read can drop a Q that is
                                # plainly legible: page 16 card 20 came back
                                # ['410'] from the stacked image but ['Q','410']
                                # from the same crop read alone. Re-decide from
                                # the original crop, escalating to a second
                                # engine when the Paddle vote is inconclusive.
                                # Age is filled later in this same pass, so it
                                # is often still empty here on a perfectly
                                # readable card. Treating that as damage forced
                                # the Tesseract crosscheck, which then marked
                                # the card deleted on a letter it imagined in
                                # the serial — HIN-447 serial 817, vote 0/8.
                                # Only a card with no gender at all is the
                                # watermark case the crosscheck is for.
                                marked, vote_hits, vote_total = _decide_deleted(
                                    ser_img,
                                    force_crosscheck=not rec.get("gender"),
                                )
                                if vote_total:
                                    rec["is_deleted"] = marked
                                    rec.setdefault("_field_sources", {})[
                                        "is_deleted"
                                    ] = (
                                        # Denominator stays the SIZE of the
                                        # reader set, not the readers actually
                                        # run: a card decided at 5/8 early-exits
                                        # here, and "5/4" would be nonsense
                                        # while "5/8" still reads correctly as
                                        # "five of the eight readers that
                                        # counted".
                                        f"serial_vote_{vote_hits}/"
                                        f"{len(DELETED_VOTE_HEIGHTS_PX) * 2}"
                                    )

                            elif field == "house" and field_texts and not _house_recovery_locked(rec):
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
                                                rec["house_no"] = _place_recovered_leading_one(cur)
                                                # A digit was reconstructed, not
                                                # read. Flag it so the value a
                                                # human checks is the one that
                                                # needs checking.
                                                rec["_house_leading_1_prepended"] = True

                            elif field == "age" and field_texts and not _age_recovery_locked(rec):
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
                    # Same decision as the normal path. The two used to differ
                    # -- this one accepted a single Paddle Q while the vote
                    # required 5/8 -- so a card could be marked deleted on the
                    # exception path and not deleted on the normal one.
                    rec["is_deleted"] = _decide_deleted(ser_img)[0]

                # Danda/double-danda lookalike fix
                house_val = rec.get("house_no", "")
                if house_val:
                    house_val = re.sub(r"^[।॥|]+(?=\d)", "1", house_val)
                    house_val = re.sub(r"(?<=\d)[।॥|]+(?=\d)", "1", house_val)
                    house_val = _strip_house_digit_glue(house_val)
                    rec["house_no"] = house_val

                # Store PaddleOCR confidence for this record
                rec["_paddle_conf"] = field_confidences

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
                # Run the review triggers here, not inside the card thread: this
                # is the first point at which every engine and every arbitration
                # has finished with the record, so a trigger sees the value that
                # will actually be emitted. Inside the pool it would inspect an
                # intermediate that the Paddle pass still rewrites.
                _review_record(record)
                record["_page_number"] = page_number
                record["_card_index"] = card_index
                record["_roll_metadata"] = page_roll_metadata
                page_records.append(record)

            records.extend(page_records)
            processed_pages.append({"page_number": page_number, "records": len(page_records)})
            page_elapsed = time.perf_counter() - page_started
            _progress(f"page {page_number} COMPLETE cards={len(card_rects)} "
                      f"records={len(page_records)} in {page_elapsed:.2f}s", force=True)
            print(f"[OCR] page={page_number} complete cards={len(card_rects)} records={len(page_records)} elapsed={page_elapsed:.2f}s", flush=True)
            # Adds the page's 30 lattice slots to the denominator, so the UI
            # can show "N of M" from the second page onward.
            _progress_page_done(page_number, len(card_rects))

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

    # Every card has now been counted, so the run is over as far as any poller
    # is concerned. Set before the final progress line so a client that reacts
    # to that line already sees done=True rather than a stalled-looking count.
    _progress_finish()

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
        # Instrumentation only. Counts of Tesseract subprocess launches, which
        # is what actually sets the runtime floor: pytesseract re-deserialises
        # the model on every call. Additive key -- safe for consumers that
        # iterate a known field set.
        "tesseract_calls": _tess_counters_snapshot(),
        "tesseract_backend": _TESS_BACKEND,
        # Populated only in shadow mode: the first few reads where tesserocr
        # disagreed with pytesseract, verbatim, so a mismatch can be diagnosed.
        "tesseract_shadow_samples": _tess_shadow_snapshot(),
    }

def _maybe_text_layer_extract(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int],
    end_page: Optional[int],
    whole_pdf: bool,
    skip_non_voter_pages: bool,
) -> dict[str, Any] | None:
    """Read a born-digital ward roll with the text-layer extractor.

    The card grid below is measured for a scanned assembly roll. A Jaipur
    Nagar Nigam file has a text layer and a different grid, so running it
    through that grid returns the wrong cards. The text-layer reader lives
    beside this module; when it is importable and the PDF is that format,
    it does the work. A scanned roll, or a text roll it does not recognise,
    falls through to the grid.
    """
    try:
        import ocr_text_pdf_api as text_api
    except Exception as exc:
        log.info("Text-layer reader is not loaded (%s); using the scanned grid", exc)
        return None
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            kind, _evidence = text_api._detect_pdf_kind(doc)
    except Exception as exc:
        # UnsupportedPdf, or a file this reader cannot classify. The scanned
        # grid remains the fallback rather than failing the request here.
        log.info("Not a text-layer ward roll (%s): %s", type(exc).__name__, exc)
        return None
    if kind != "text":
        return None
    log.info("Born-digital ward roll %s: reading it with the text-layer extractor", filename)
    return text_api.extract_pdf_text(
        pdf_bytes, filename, start_page, end_page, whole_pdf, skip_non_voter_pages,
    )


def extract_pdf_ocr(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    whole_pdf: bool = False,
    skip_non_voter_pages: bool = True,
) -> dict[str, Any]:
    """Extract OCR from PDF. A text-layer ward roll is read by that extractor; everything else by the scanned grid."""
    text_result = _maybe_text_layer_extract(
        pdf_bytes, filename, start_page, end_page, whole_pdf, skip_non_voter_pages,
    )
    if text_result is not None:
        return text_result
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
        # Requested vs effective: a tesserocr request that fell back to
        # pytesseract (library missing) shows up here as two different values.
        "tesseract_backend": _tess_backend_info(),
    }


@app.get("/ocr/progress")
async def ocr_progress() -> dict[str, Any]:
    """Live card progress for the request currently running.

    Read-only and observational: it reads the in-memory counter and changes
    nothing. The counter is reset at the start of every /ocr/extract call, so
    a caller arriving between requests sees zeros rather than a stale count
    from the previous run.

    cards_done counts grid *positions* processed, which is not the same as
    records found: _voter_card_rects() returns 30 positions per page whether
    or not a cell holds a card, so a roll of 600 positions can yield 531
    records. Both numbers are reported separately and must not be conflated
    under one label.

    Known limitation: _OCR_LOCK serialises whole requests, so this describes
    *a* run, not necessarily the caller's. With the single worker this
    service runs by default there is never more than one run in flight, so
    the distinction does not arise. Distinguishing them would mean threading
    a job id through the extraction path, which is not a read-only change.
    """
    snap = _progress_snapshot()
    return {
        **snap,
        "running": not snap["done"] and snap["updated_at"] > 0,
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
    # Rebase the progress clock on the request, not on the process. _PROGRESS_START
    # is module-level, so a long-lived server made every log line report process
    # age -- the first page logged "+438.9s" having done no work at all, which
    # made real per-request cost impossible to read.
    global _PROGRESS_START
    _PROGRESS_START = time.time()

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



@app.post("/ocr/cover")
async def ocr_cover_endpoint(
    pdf_file: UploadFile = File(..., description="Electoral-roll PDF; only page 1 is read"),
) -> Response:
    """Everything the cover page says: header fields and the printed counts.

    Separate from /ocr/extract on purpose. Reading one page takes seconds,
    card extraction takes hours, and the printed male / female / total counts
    are what the card results are checked against -- so they must not wait
    behind that work. This endpoint does not take _OCR_LOCK: it is a single
    Tesseract pass with no shared engine state.
    """
    from ocr_cover import extract_cover

    filename = pdf_file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported")
    raw = await pdf_file.read(MAX_PDF_BYTES + 1)
    if not raw:
        raise HTTPException(400, "PDF file is empty")
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit")
    try:
        result = await asyncio.to_thread(extract_cover, raw, filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        log.exception("Cover page extraction failed")
        raise HTTPException(500, f"Cover extraction failed: {exc}") from exc
    result["filename"] = filename
    return Response(
        content=json.dumps(result, ensure_ascii=False) + "\n",
        media_type="application/json",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("OCR_API_HOST", "0.0.0.0"),
        port=int(os.getenv("OCR_API_PORT", "8082")),
    )
