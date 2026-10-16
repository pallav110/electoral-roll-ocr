#!/usr/bin/env python3
"""FastAPI service for born-digital (text-layer) electoral-roll PDFs.

Run directly:

    python ocr_text_pdf_api.py

Or with Uvicorn:

    uvicorn ocr_text_pdf_api:app --host 0.0.0.0 --port 8083

This is a sibling of ``ocr_pdf_api.py`` (port 8082), not a replacement. That
service handles image-scanned rolls and works by rasterising each card and
OCR-ing it. This one handles rolls that were generated digitally and carry a
real text layer, and reads most fields straight out of that layer.

Why a second service rather than a branch inside the first
---------------------------------------------------------
``ocr_pdf_api`` slices each page into a fixed 3x10 grid of card rectangles
using ratios calibrated against one ECI template, and finds no text-layer
content because it never asks for one. A born-digital roll breaks that
assumption three ways at once:

  * It has a different grid. The Jaipur roll below prints 3 columns x 8-9
    rows, not 3x10, and its rows are not evenly spaced -- measured gaps run
    72.3, 90.9, 144.7 and 183.7pt depending on where the page happens to
    break. A hardcoded lattice cannot represent that.
  * It mirrors the field boxes. The ECI template prints the EPIC in a box at
    the card's top-right; this one prints it top-left. The old service reads
    the EPIC from card-relative x 0.69-1.00, which on this template lands on
    page x 138-197 while the real EPIC sits at x 72-134 -- no overlap at all.
  * It fails silently. ``_page_looks_like_voter_grid`` probes twelve fixed
    positions, finds no EPIC on any of them, and reports every page as
    skipped. The caller gets HTTP 200 with zero records, which reads like
    "this roll has no voters" rather than "wrong service".

So the card rectangles here are derived per document from that document's own
ID-token positions, and a wrong-template PDF is rejected loudly instead of
returning an empty result.

Where each field comes from, and why
--------------------------------------
The text layer is reliable for exactly one thing and useless for another, so
each field is taken from whichever source is trustworthy:

  * ASCII (serial, ID number, house number, age) comes from the text layer
    verbatim. It is exact there. OCR is *not* used for these: a probe of this
    template read an age of 53 as "553", and a spurious digit is precisely
    the failure mode that poisons a voter roll.
  * Devanagari (names, relation labels) comes from Tesseract, reading the
    vector-rendered crop. The stored Devanagari is destroyed -- the fonts are
    embedded with WinAnsi encoding, which has no Devanagari range, so
    shaping never runs and dependent vowel signs come back as Latin garbage
    ("राज्य निर्वाचन आयोग" extracts as "रपजय ननवपरचन आययग"). The *rendered*
    glyphs are perfectly good, so OCR of the render recovers the names
    cleanly. Reusing the existing, tuned field readers is why this works.

Card geometry, measured
------------------------
Everything is positioned relative to the ID token, which is the one mark
every card in this template reliably carries. Measured over all 1363 cards in
the sample roll, these offsets are exact and never vary:

    column pitch      173.50 pt, stable to 0.1pt across every page
    ID token at       column origin + 37.5 pt
    card box          173.50 x 71.85 pt, sitting 3.03 pt above the ID token
    serial number     dx +14, dy -1   (3 digits, present on every card)
    house number      dx +59, dy +42  (may carry a /suffix, e.g. 202/1)
    age               dx +28, dy +53

Serials run continuously across the whole part (1, 2, 3 ... across pages),
which is what identifies a card when the serial itself fails to extract.

Measured against the sample PDFs in sample-pdfs/:

  * 2026-EROLLGEN-S24-53-SIR-...300-WI.pdf -- scanned, 22pp, zero embedded
    fonts, page.get_text() returns nothing. This service rejects it and
    points the caller at :8082.
  * JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf -- born-digital, 62pp,
    4 WinAnsi fonts, ~500 words/page. This service extracts it: 1363 cards
    over 59 voter pages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

# Tesseract's OpenMP must not fight the card worker pool: 8 workers each
# spawning OpenMP threads is massive oversubscription. Same reason, same
# setting as the sibling service.
os.environ["OMP_THREAD_LIMIT"] = "1"

import cv2
import fitz
import httpx
import numpy as np
import pytesseract
from PIL import Image
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

# The downstream half of this service -- name canonicalisation, confidence
# scoring, review flagging, the public record shape -- is deliberately shared
# with ocr_pdf_api rather than copied. Two copies of that logic drift apart,
# and the caller's schema has to match exactly across both services.
#
# If this file is ever run from a directory where ocr_pdf_api is not
# importable, say so loudly instead of degrading: a partial service that
# invents its own confidence numbers is worse than one that refuses to start.
try:
    import ocr_pdf_api as _base
except Exception as _exc:  # pragma: no cover - import guard
    print(
        "FATAL: ocr_pdf_api.py must be importable from "
        f"{Path(__file__).resolve().parent} ({type(_exc).__name__}: {_exc}).\n"
        "It supplies the shared record/confidence/review layer. Keep both "
        "files in the same directory.",
        file=sys.stderr,
    )
    raise

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
# Module-level, and therefore shared across concurrent requests -- the same
# caveat the sibling service carries. _progress_reset() clears it at the start
# of every run, so a second concurrent extraction overwrites the first run's
# counters and two runs interleave. It is a progress *display*, never an input
# to the result, so this is a cosmetic hazard rather than a correctness one;
# a caller that needs per-run progress should serialise its requests or read
# the terminal log instead.

_PROGRESS_LOCK = threading.Lock()
_PROGRESS_SNAPSHOT: dict[str, Any] = {}


def _progress_reset(pages_total: int = 0) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT.clear()
        _PROGRESS_SNAPSHOT.update({
            "phase": "starting",
            "message": "starting",
            "done": False,
            "pages_total": pages_total,
            "pages_done": 0,
            "cards_total": 0,
            "cards_done": 0,
            "records": 0,
            "current_page": None,
            "started_at": time.time(),
            "updated_at": time.time(),
        })


def _progress(msg: str, **fields: Any) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["message"] = msg
        _PROGRESS_SNAPSHOT["updated_at"] = time.time()
        _PROGRESS_SNAPSHOT.update(fields)


def _progress_page_done(page_number: int, cards_on_page: int) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["pages_done"] += 1
        _PROGRESS_SNAPSHOT["cards_done"] += cards_on_page


def _progress_finish() -> None:
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["done"] = True
        _PROGRESS_SNAPSHOT["phase"] = "done"


def _progress_snapshot() -> dict[str, Any]:
    with _PROGRESS_LOCK:
        snap = dict(_PROGRESS_SNAPSHOT)
    if snap.get("started_at"):
        snap["elapsed"] = round(time.time() - snap["started_at"], 1)
    return snap


# ---------------------------------------------------------------------------
# Template geometry
# ---------------------------------------------------------------------------
# Measured across all 1363 cards of
# sample-pdfs/JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf. These are
# offsets from an anchor token (see _card_anchors), not page ratios: page
# ratios are what broke the sibling service, because this template's rows
# are not evenly spaced and its column count varies by page.

# Horizontal pitch between card columns, in points. Stable to 0.1pt on every
# page of the sample, and equal to the printed card width.
COLUMN_PITCH_PT = 173.5
# Offset of the ID token's left edge from its column's origin.
CARD_ID_DX = 37.5
# The ID token is not at the top of the printed box; these lift the crop up
# to the box's real top edge and give it the box's height.
CARD_TOP_OFFSET_PT = 3.03
CARD_WIDTH_PT = 173.5
CARD_HEIGHT_PT = 71.85
# Render DPI. The sibling service probes at 200 and reads at 300; a card is
# only ~72pt tall, so 300 DPI keeps Devanagari strokes above the size where
# Tesseract's LSTM starts dropping matras.
CARD_RENDER_DPI = 300
# The header is small print; the card render DPI is plenty for it too.
_HEADER_RENDER_DPI = CARD_RENDER_DPI
# How far the OCR'd constituency name may drift from the text layer's own
# (corrupted) spelling before the two are reported as disagreeing.
_HEADER_NAME_MAX_EDITS = 2

# Field rows inside a card, in card-relative points. The ROWS are exact and
# hold for all 1363 cards; the COLUMNS are not. The roll carries two font
# encodings, and the later pages shift every field left by up to 9pt (serial
# 14.4 -> 8.7, age 28.2 -> 19.2, house 59.5 -> 50.5). So a field is picked by
# its row, which never moves, and the x bound exists only to stop the window
# reaching into the card next door -- which sits a full 173.5pt away.
# Measured across the whole roll; see tests/test_ocr_routing.py.
SERIAL_WINDOW = (0.0, 30.0, -3.0, 6.0)
HOUSE_WINDOW = (40.0, 120.0, 40.0, 48.0)
AGE_WINDOW = (12.0, 45.0, 52.0, 60.0)
# The supplement pages (from page 54 in this roll) print the deletion letter
# inside the serial box, immediately left of the number: "R 1144", "S 1162",
# "E 1169". Measured at dx -2.2, dy 3.9 from the card origin. The earlier
# guess put this window on the card's right edge, which is where the next
# card's letter sits, so a card was marked with its neighbour's letter and
# its own letter was missed.
_SERIAL_MARKER_WINDOW = (-14.0, 8.0, -2.0, 10.0)
_SERIAL_MARKERS = ("E", "S", "R")
# What the roll's own legend says each marker means. From the footer printed
# on page 54: "* E-Deleted Due To Death | S-Deleted Due Shifted |
# R-Deleted to Repeatition".
_DELETION_MARKER_MEANING = {
    "E": "deleted due to death",
    "S": "deleted due to shifted",
    "R": "deleted due to repetition",
}
# The EPIC-prefix reference the card prints beside the house number. It is a
# placeholder in this roll, never an actual house number.
_EPIC_PREFIX_SENTINELS = frozenset({"999999"})
# A house number is anything on the house row that carries a digit and is made
# only of digits, letters, separators and Devanagari. Tight enough to reject
# the row's own label text ("मकरन ससखजर:", which has no digit at all), loose
# enough to keep "202/1", "48-49", "5-A" and "सर-9".
_HOUSE_RE = re.compile(r"^[\d०-९][\d०-९A-Za-z\-/.,'ऀ-ॿ]*$")
# The house label is frequently welded to its value by the text layer:
# "सर-9", "बर-36", "जर1/42", "पपकट नस.137". The value is the ASCII digits that
# follow the last Devanagari run; the label in front of it is dropped, because
# shipping "सर-9" as a house number corrupts the roll just as badly as an empty
# one. Digits are extracted, never reordered, so "48-49" stays "48-49".
_HOUSE_LABEL_RE = re.compile(r"^[ऀ-ॿ]+[\s\-/.]*")
# Devanagari digits are used occasionally in this roll and map onto ASCII.
_DEVA_DIGIT_TRANS = {ord(c): str(i) for i, c in enumerate("०१२३४५६७८९")}

# A voter ID in this template takes two shapes: the usual three letters and
# seven digits (IQT0751719), and a slash-delimited form (RJ/07/039/516332).
# Both are anchored on, and both are anchored on only if they look like an
# ID -- a bare number is a serial or a house number and must not be mistaken
# for one, or every card on the page becomes its own anchor.
ID_PATTERN = re.compile(r"^(?:[A-Z]{3}\d{7}|[A-Z]{2}/\d{2}/\d{3}/\d{6})$")

# Minimum ID tokens on a page for it to count as a voter page. A single
# match is far too weak: the cover page and the trailing summary page of this
# template both carry stray codes, and three of the sample's 62 pages carry
# no card at all.
MIN_IDS_PER_VOTER_PAGE = 4

# Page-relative header band. This template prints its part identity (ward,
# part, year) above the first card row, and the row's y varies with the page
# header, so the band is expressed as a fraction of page height.
HEADER_BAND_TOP = 0.0
HEADER_BAND_BOTTOM = 0.155


def _card_anchors(page: fitz.Page) -> list[tuple[float, float, str]]:
    """Locate every voter card on a page, anchored on its ID token.

    Returns (x_origin, y_origin, id_text) for each card found, in reading
    order (top-to-bottom, then left-to-right).

    The column origin is derived from the ID token rather than trusted from
    the page: the token sits at a fixed offset inside its card, so snapping
    to the nearest column of a derived lattice recovers the card's true box
    without assuming how many columns the page has.
    """
    words = page.get_text("words")
    ids = [w for w in words if ID_PATTERN.match(w[4].strip())]
    if not ids:
        return []

    # Cluster ID x-positions into columns. Columns are a fixed pitch apart,
    # so anything within a small fraction of the pitch is the same column.
    anchors: list[tuple[float, float, str]] = []
    for w in ids:
        text = w[4].strip()
        origin_x = w[0] - CARD_ID_DX
        anchors.append((origin_x, w[1] - CARD_TOP_OFFSET_PT, text))

    # Reading order: rows are separated by tens of points, columns by ~173.
    # Sorting on y with a tolerance derived from the row height keeps a
    # column's three cards from interleaving with the next row's.
    anchors.sort(key=lambda a: (round(a[1] / (CARD_HEIGHT_PT / 2)), a[0]))
    return anchors


def _card_sized_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Drawn boxes that are the size of one voter card.

    A card's left edge is not a fixed distance from its voter ID. A longer
    serial, or a supplement page whose serial box is wider, pushes the ID
    to the right. Cropping from the ID then slices the first letter off
    every label, and the name is dropped because "नाम:" no longer looks
    like a label. The border drawn around the card does not move, so that
    is the box to render.
    """
    found: list[fitz.Rect] = []
    seen: set[tuple[float, float, float, float]] = set()
    for drawing in page.get_drawings():
        rects: list[fitz.Rect] = []
        outer = drawing.get("rect")
        if outer is not None:
            rects.append(outer)
        for item in drawing.get("items") or []:
            if item[0] == "re":
                rects.append(item[1])
        for rect in rects:
            if not (140 < rect.width < 210 and 55 < rect.height < 100):
                continue
            key = (
                round(rect.x0, 1), round(rect.y0, 1),
                round(rect.x1, 1), round(rect.y1, 1),
            )
            if key in seen:
                continue
            seen.add(key)
            found.append(rect)
    return found


def _drawn_card_rect(
    rects: list[fitz.Rect], id_x: float, id_y: float,
) -> Optional[fitz.Rect]:
    """Smallest card-sized box that contains this voter ID, if one is drawn."""
    best: Optional[tuple[float, fitz.Rect]] = None
    for rect in rects:
        if rect.x0 - 1 <= id_x <= rect.x1 + 1 and rect.y0 - 1 <= id_y <= rect.y1 + 1:
            area = rect.width * rect.height
            if best is None or area < best[0]:
                best = (area, rect)
    return None if best is None else best[1]


def _cards_on_page(page: fitz.Page) -> list[tuple[fitz.Rect, str, float, float]]:
    """Return (render_rect, id_text, field_x, field_y) for every card.

    field_x/field_y stay anchored on the voter ID. Serial, house and age
    were measured from that point and must not shift when the rendered box
    grows to the left to include a label the ID-based crop would have cut.
    """
    anchors = _card_anchors(page)
    if len(anchors) < MIN_IDS_PER_VOTER_PAGE:
        return []
    drawn = _card_sized_rects(page)
    cards = []
    for origin_x, origin_y, text in anchors:
        fallback = fitz.Rect(
            origin_x, origin_y,
            origin_x + CARD_WIDTH_PT, origin_y + CARD_HEIGHT_PT,
        )
        id_x = origin_x + CARD_ID_DX
        id_y = origin_y + CARD_TOP_OFFSET_PT
        box = _drawn_card_rect(drawn, id_x, id_y)
        # Only adopt a drawn box that reaches at least as far left as the
        # ID-based crop. A box that starts further right would hide the name.
        rect = box if box is not None and box.x0 <= fallback.x0 + 1.0 else fallback
        cards.append((rect, text, origin_x, id_y))
    return cards


def _page_looks_like_voter_page(page: fitz.Page) -> bool:
    """True if this page carries a card grid this service can read."""
    return len(_anchors_count_for_probe(page)) >= MIN_IDS_PER_VOTER_PAGE


def _anchors_count_for_probe(page: fitz.Page) -> list[tuple[float, float, str]]:
    try:
        return _card_anchors(page)
    except Exception as exc:
        log.debug("anchor probe failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# PDF-type detection
# ---------------------------------------------------------------------------

class UnsupportedPdf(ValueError):
    """Raised when a PDF is not the born-digital template this service reads."""


def _detect_pdf_kind(doc: fitz.Document) -> tuple[str, dict[str, Any]]:
    """Classify a PDF as text-layer or scanned, and prove the template.

    Returns ("text" | "scanned", evidence). Raises UnsupportedPdf when the
    document is a born-digital roll this service cannot read -- a different
    template is an error, not an empty result.
    """
    sampled = [doc[i] for i in range(min(doc.page_count, 6))]
    text_pages = 0
    total_chars = 0
    for page in sampled:
        chars = len(page.get_text().strip())
        total_chars += chars
        if chars > 200:
            text_pages += 1

    evidence = {
        "pages_sampled": len(sampled),
        "pages_with_text": text_pages,
        "total_text_chars": total_chars,
    }

    if text_pages == 0:
        evidence["reason"] = (
            "No extractable text layer on the sampled pages. This looks like "
            "an image-scanned roll."
        )
        return "scanned", evidence

    # Text layer exists, so confirm it is actually this template's card grid
    # before doing any work. Probe a spread of pages, not just the first: a
    # document with a text cover and scanned cards inside would otherwise be
    # mistaken for a born-digital roll.
    best_page, best_count = None, 0
    for pno in range(doc.page_count):
        count = len(_anchors_count_for_probe(doc[pno]))
        if count > best_count:
            best_page, best_count = pno, count
        if best_count >= MIN_IDS_PER_VOTER_PAGE * 3:
            break

    evidence["best_page"] = (best_page + 1) if best_page is not None else None
    evidence["cards_on_best_page"] = best_count

    if best_count < MIN_IDS_PER_VOTER_PAGE:
        evidence["reason"] = (
            "A text layer is present, but no page carries a recognisable "
            "voter-card grid in this template. This service handles the "
            "born-digital Jaipur-style roll; ocr_pdf_api on port 8082 handles "
            "the scanned ECI roll."
        )
        raise UnsupportedPdf(
            "Unsupported template: text layer present but no voter-card grid "
            "found (best page had %d cards, need %d). If this is a scanned "
            "roll, use ocr_pdf_api on port 8082 instead."
            % (best_count, MIN_IDS_PER_VOTER_PAGE)
        )

    return "text", evidence


# ---------------------------------------------------------------------------
# Text-layer field readers
# ---------------------------------------------------------------------------

def _words_near(words: list[tuple], origin_x: float, origin_y: float,
                window: tuple[float, float, float, float]) -> list[tuple]:
    """Words whose top-left falls inside a card-relative window."""
    dx0, dx1, dy0, dy1 = window
    out = []
    for w in words:
        dx = w[0] - origin_x
        dy = w[1] - origin_y
        if dx0 <= dx <= dx1 and dy0 <= dy <= dy1:
            out.append(w)
    return out


def _flag_broken_serial_run(
    page_records: list[dict[str, Any]], prior_records: list[dict[str, Any]],
) -> None:
    """Flag a page whose serials do not run consecutively.

    Serials in this template run 1..N across the roll, page after page. That
    continuity is the cheapest available proof that the field windows landed
    on the right words: if a page's serials restart, repeat or run backwards,
    the windows are reading something other than the serial row, and every
    value on that page is suspect.

    Only WITHIN-page continuity is checked, deliberately. The roll appends a
    deleted-voters addendum whose cards repeat earlier voters and restart
    numbering in their own block (pages 54-61 here, serials 57, 177, 435,
    ...), so demanding a run across every page would flag the whole addendum
    as corrupt when it is a separate, correctly numbered section.

    This only ADDS a review reason. It never rewrites a value, because a
    discontinuous run says the page is unreliable, not what the right answer
    is. Leaving this silent is what let pages 54-62 emit a house number of
    "999999" for a voter who has none.
    """
    if not page_records:
        return

    def _serial_of(record: dict[str, Any]) -> Optional[int]:
        raw = str(record.get("sno", "")).strip()
        return int(raw) if raw.isdigit() else None

    page_serials = [_serial_of(r) for r in page_records]
    usable = [s for s in page_serials if s is not None]
    if len(usable) < 2:
        return

    # Within the page, serials must strictly increase. Cards are emitted in
    # reading order, which is the order the serials were printed in. A GAP is
    # normal -- the roll skips cards, and the deleted addendum restarts
    # numbering per section -- but a serial that goes backwards or repeats
    # means the windows read something other than the serial row.
    breaks = [
        (a, b) for a, b in zip(usable, usable[1:]) if b <= a
    ]
    if not breaks:
        return

    detail = ", ".join("%d->%d" % pair for pair in breaks[:3])
    if len(breaks) > 3:
        detail += ", +%d more" % (len(breaks) - 3)
    for record in page_records:
        _base._mark_review(
            record,
            "serial_run_broken: %d of %d consecutive pairs do not increase "
            "(%s)" % (len(breaks), len(usable) - 1, detail),
        )


def _clean_house(raw: str) -> str:
    """Reduce a raw house-row token to the house number itself.

    Strips the Devanagari label the text layer welds onto the value, folds
    Devanagari digits to ASCII, and drops stray punctuation. The digits and
    their separators are kept in the order the roll printed them.
    """
    text = raw.strip()
    # Fold the digits FIRST: a value written wholly in Devanagari numerals
    # ("८८" = 88) looks entirely like label, and translating before the strip
    # is what lets the strip see it for the value it is. Do it the other way
    # round and the label regex eats the digits.
    text = text.translate(_DEVA_DIGIT_TRANS)
    # Twice, because a token can carry two label runs: "पपकट नस.137" -> "नस.137"
    # -> "137". Repeating is safe -- the pattern is anchored, so once a value
    # is reached it stops matching.
    text = _HOUSE_LABEL_RE.sub("", text)
    text = _HOUSE_LABEL_RE.sub("", text)
    return text.strip(" .-/,")


def _ascii_values_from_text_layer(
    page: fitz.Page, origin_x: float, origin_y: float,
) -> dict[str, str]:
    """Read serial, house number and age straight from the text layer.

    This is the path that makes the hybrid worth having. These fields are
    pure ASCII and the layer stores them exactly; OCR on the same card read
    an age of 53 as "553", which is the kind of error that silently corrupts
    a roll.
    """
    words = page.get_text("words")
    result: dict[str, str] = {}

    serials = _words_near(words, origin_x, origin_y, SERIAL_WINDOW)
    serial_text = [w[4].strip() for w in serials if re.fullmatch(r"\d{1,4}", w[4].strip())]
    if serial_text:
        # The serial is 1-2 digits in this roll; a longer run means the row
        # caught something else, and the leftmost token is the serial.
        result["serial"] = serial_text[0]

    # A card marked E/S/R has been struck off the roll. The letter is the only
    # record of that, and it is printed on the far side of the photo box, so
    # it needs its own lookup.
    markers = [
        w[4].strip() for w in _words_near(
            words, origin_x, origin_y,
            _SERIAL_MARKER_WINDOW)
        if w[4].strip() in _SERIAL_MARKERS
    ]
    if markers:
        result["deletion_marker"] = markers[0]

    houses = _words_near(words, origin_x, origin_y, HOUSE_WINDOW)
    # House numbers here are not plain integers. The roll prints "202/1",
    # "5/2/", "48-49", "5-A" and even a labelled "सर-9" or "पपकट नस.137",
    # so a strict \d+ test throws away real, readable values. _HOUSE_RE
    # decides what is a house number; _clean_house strips the label off it.
    house_text = [_clean_house(w[4].strip()) for w in houses]
    house_text = [t for t in house_text if t and _HOUSE_RE.match(t)]
    # The card prints an EPIC-prefix reference on the same visual line as the
    # house number ("मकरन ससखजर: 999999"), and it is always the placeholder
    # 999999. It is a constant, never a real house number, so it is dropped
    # here rather than trusted downstream -- and dropped even when it is the
    # only token on the row, because an empty house number is honest where a
    # placeholder is a lie.
    house_text = [t for t in house_text if t not in _EPIC_PREFIX_SENTINELS]
    if house_text:
        result["house_no"] = house_text[0]

    ages = _words_near(words, origin_x, origin_y, AGE_WINDOW)
    age_text = [w[4].strip() for w in ages if re.fullmatch(r"\d+", w[4].strip())]
    age_text = [t for t in age_text if 18 <= int(t) <= 120]
    if age_text:
        # Ages are 2 digits; prefer a 2-digit read over a clipped fragment.
        two = [t for t in age_text if len(t) == 2]
        result["age"] = two[0] if two else age_text[0]

    return result


# ---------------------------------------------------------------------------
# Rendered-card OCR (Devanagari only)
# ---------------------------------------------------------------------------
# The render is vector, so the Devanagari draws correctly even though the
# stored text layer is mangled. Reading the render is therefore the only way
# to recover names, and the sibling service's field readers already know how
# to pull a name and relation out of a rendered card, so they are reused
# rather than reinvented.

# Which card-relative band holds the Devanagari block. Measured on this
# template's cards, not borrowed from the sibling's constants: the name /
# relation / house / gender / age block occupies the left ~60% of the card
# below the header row. Kept as a fraction of card size.
DEVANAGARI_BAND = (0.02, 0.14, 0.68, 1.00)


def _render_card(page: fitz.Page, rect: fitz.Rect) -> Optional[np.ndarray]:
    """Rasterise one card at CARD_RENDER_DPI and clean it for OCR."""
    try:
        png = page.get_pixmap(dpi=CARD_RENDER_DPI, clip=rect, alpha=False).tobytes("png")
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        return _base._remove_outer_card_lines(img)
    except Exception as exc:
        log.debug("card render failed: %s", exc)
        return None


def _devanagari_fields_from_render(page: fitz.Page, rect: fitz.Rect) -> tuple[dict[str, str], Optional[float]]:
    """OCR the Devanagari block of one rendered card.

    Returns (fields, mean_confidence). `fields` carries the voter's name and
    relation; gender is read too, since it is printed on the same row as age
    and is Devanagari. ASCII fields are deliberately not read here.
    """
    img = _render_card(page, rect)
    if img is None:
        return {}, None

    h, w = img.shape[:2]
    x0, y0, x1, y1 = DEVANAGARI_BAND
    band = img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    if band.size == 0:
        return {}, None

    ok, enc = cv2.imencode(".png", band)
    if not ok:
        return {}, None

    lines, conf = _base._extract_text_with_tesseract(enc.tobytes())
    if not lines:
        return {}, None

    fields = _parse_hindi_card_lines(lines)
    return fields, conf


# Hindi labels as they come back from OCR of this template. Kept as a tuple
# because OCR varies the spacing and occasionally the final character.
_LABEL_NAME = ("नाम",)
_LABEL_FATHER = ("पिता",)
_LABEL_HUSBAND = ("पति",)
_LABEL_MOTHER = ("माता",)
_LABEL_OTHER = ("अन्य",)
_LABEL_HOUSE = ("मकान",)
_LABEL_AGE = ("आयु",)
_LABEL_GENDER = ("लिंग",)

# Longest-first, so "पिता" is tested before any shorter label that could
# match a prefix of it.
_ALL_LABELS: tuple[str, ...] = (
    "नाम",
    "पिता का नाम",
    "पति का नाम",
    "माता का नाम",
    "अन्य",
    "पिता",
    "पति",
    "माता",
    "मकान संख्या",
    "मकान",
    "आयु",
    "लिंग",
)

def _nfc(value: str) -> str:
    """Normalise Devanagari to NFC for reliable label comparison.

    Tesseract and hand-written literals disagree about precomposed
    characters: "कि" can arrive as U+0915 U+093F or as a single codepoint,
    and "क्ष" as a codepoint or as base + virama + consonant. Comparing raw
    strings then fails on a line that is visibly correct. NFC puts both sides
    in one representation so the label tests below compare what they mean to.
    """
    return unicodedata.normalize("NFC", value)


# Matras that occupy their own codepoint instead of combining, and so are
# not caught by unicodedata.combining(). Without this, a written "ता म:" and
# an OCR'd "ताम:" keep different skeletons, and the label comparison fails on
# exactly the string it was written to survive.
_SPACING_MATRAS = "ऺऻ़ऽॕॖॗॢॣॺॻॼॽ"

# Everything that separates a label from its value, so that "ता म: धन्नी",
# "ताम; धन्नी" and "नाम: धन्नी" all reduce the same way. OCR reads the
# printed colon as a semicolon often enough to matter: it is why one of the
# two voters whose names were being dropped read "ताम; धन्नी".
_SEPARATORS = ":;"


def _is_skeleton_char(ch: str) -> bool:
    """True if this character contributes a consonant to the skeleton."""
    if unicodedata.combining(ch) or ch in _SPACING_MATRAS:
        return False
    if ch.isspace() or ch in _SEPARATORS or ch in "।॥,/-":
        return False
    # Keep only Devanagari consonants and independent vowels; drop Latin
    # and digits so a value can never be mistaken for a label.
    return "ऀ" <= ch <= "ॿ"


def _skeleton(value: str) -> str:
    """Reduce Devanagari to its consonant skeleton.

    Drops every combining mark (matras, viramas, anusvara) and all
    whitespace/punctuation, so "नाम:" and "ता म:" both reduce to the
    consonants they are meant to spell. This is what lets a label be
    recognised despite OCR varying its vowel signs, inserting a stray
    space, or misreading one glyph.
    """
    return "".join(ch for ch in _nfc(value) if _is_skeleton_char(ch))


def _skeleton_positions(nfc_line: str) -> list[int]:
    """Original indices of the characters _skeleton would keep, in order."""
    return [i for i, ch in enumerate(nfc_line) if _is_skeleton_char(ch)]


def _edit_distance_within(a: str, b: str, cap: int) -> Optional[int]:
    """Levenshtein distance between a and b, or None if it exceeds cap."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, start=1):
            cur[j] = min(
                prev[j] + 1,                       # deletion
                cur[j - 1] + 1,                    # insertion
                prev[j - 1] + (ca != cb),          # substitution
            )
            best = min(best, cur[j])
        if best > cap:
            return None
        prev = cur
    return prev[len(b)] if prev[len(b)] <= cap else None


def _align_span(line_skel: str, label_skel: str,
                max_edits: int = 1) -> Optional[int]:
    """Edit distance from a line's pre-colon skeleton to a label's.

    OCR does not read the printed labels back letter-perfect: "नाम:" comes
    back as "ता म:", with the first consonant misread and a space inserted.
    Requiring an exact match would reject a line whose value is perfectly
    readable and silently drop the name, which is how two real voters lost
    their names before this was fixed.

    The tolerance is deliberately one edit, not more. "पति" and "पिता" -- and
    so "पति का नाम" and "पिता का नाम" -- are themselves a single edit and a
    single character apart, so no threshold above one keeps a husband's card
    out of the father field. Callers therefore look for an exact match across
    every label first and only then fall back to this.
    """
    label_len = len(label_skel)
    if label_len == 0:
        return None
    if abs(len(line_skel) - label_len) > max_edits:
        return None
    return _edit_distance_within(line_skel, label_skel, max_edits)


def _label_end_index(nfc_line: str) -> int:
    """Index of the character separating a label from its value, or -1.

    Both marks count: OCR renders the printed colon as a semicolon often
    enough to matter -- "नाम: धन्नी" comes back as "ताम; धन्नी" for one of
    the two voters whose names were being dropped.
    """
    found = [idx for sep in _SEPARATORS
             for idx in (nfc_line.find(sep),) if idx >= 0]
    return min(found) if found else -1


def _label_of_line(nfc_line: str) -> Optional[tuple[str, int]]:
    """Return (label, value_start) if the line begins with a field label.

    Returns None when the line carries no recognised label, which is how
    stray header text above the name is dropped.

Matching is done on skeletons (see _skeleton) and tolerates one garbled
    glyph (see _align_span), because OCR does not read these labels back
    letter-perfect -- "नाम:" comes back as "ता म:". An exact match would
    reject a line whose value is perfectly readable and silently drop the
    name, which is how two real voters lost their names before this was
    fixed.

    The separator bounds the label: the text before it is the label and the
    text after it is the value, so alignment only ever runs inside that
    bound. That is what stops a bare name being read as a label -- "नर्मदा"
    has no separator at all, and "रामचन्द्र" has no room for one in its first
    two consonants. A label printed without a separator still matches, as
    long as nothing alpha follows it.
    """
    colon = _label_end_index(nfc_line)
    before = nfc_line if colon < 0 else nfc_line[:colon]
    before = before[:24]

    positions = _skeleton_positions(before)
    head_skel = "".join(before[i] for i in positions)
    if len(head_skel) < 2:
        return None

    # Longest labels first (see _ALL_LABELS ordering), so "पति का नाम" is
    # preferred over the bare "पति". Every label is tried at tolerance zero
    # before any is tried at tolerance one: an exact match is the only thing
    # that separates "पति का नाम" from "पिता का नाम", which are a single
    # edit apart.
    for max_edits in (0, 1):
        for label in _ALL_LABELS:
            label_skel = _skeleton(label)
            if len(label_skel) < 2:
                continue
            if _align_span(head_skel, label_skel, max_edits) is None:
                continue

            if colon < 0 and len(_skeleton(nfc_line)) > len(head_skel):
                # No colon, but something alpha still follows: this is a
                # fragment of a longer word, not a label.
                continue

            value_start = colon + 1 if colon >= 0 else len(nfc_line)
            return _nfc(label), value_start
    return None


def _clean_value(text: str) -> str:
    """Trim the separators OCR leaves around a field value."""
    out = re.sub(r"^[\s:;।\-–—ँंः]+", "", text)
    return out.strip()


def _canonical_gender(text: str) -> str:
    """Map only explicit gender vocabulary to the three stored values.

    Municipal rolls use older Hindi words (``स्त्री``, ``मर्द``) as well as
    ``महिला`` and ``पुरुष``, and they also print ``तृतीय लिंग``.  This is
    called only for the value following the gender label, never on names or
    relation text.

    तृतीय लिंग is tested first: both त are required by the pattern, which is
    what separates it from names like मैत्रेय, क्षत्रिय and शत्रिया.
    """
    compact = re.sub(r"[\s:;ः,.|]", "", _nfc(text or ""))
    if _base.THIRD_GENDER_RE.search(compact) or re.search(
            r"\bthird\s*gender\b", compact, re.IGNORECASE):
        return "तृतीय लिंग"
    if (re.search(r"प[ुू]र[ुू]?[षश]", compact)
            or re.search(r"म[रड][्]?द", compact)
            or re.search(r"\b(?:male|mard)\b", compact, re.IGNORECASE)
            or re.search(r"(?:^|[^ऀ-ॿ])नर(?:$|[^ऀ-ॿ])", compact)):
        return "पुरुष"
    if (re.search(r"म[ऀ-ॿ]*ल[ऀ-ॿ]*ा", compact)
            or re.search(r"स्त्री|स्त्रि|स्तरी", compact)
            or "औरत" in compact or "नारी" in compact
            or re.search(r"\b(?:mahila|female|aurat|naari)\b", compact, re.IGNORECASE)):
        return "महिला"
    return ""


def _trim_trailing(text: str) -> str:
    """Drop the punctuation OCR leaves at the end of a value.

    Card borders and the next field's edge bleed into the value, so names
    come back as "धन्नी ः", "बदाम देवी ः", "राम चंद्र चौधरी. *" and
    "मालू राम चौधरी «". A cut-off glyph comes back as a consonant with a
    dangling virama ("सुरेश ल्"), which is never how a printed name ends.
    """
    out = re.sub(
        r"[\s।॥|/\\,ँः.:;!\-*_=+&%#@\"'()\[\]{}<>?~`^$0-9०-९«»“”]+$",
        "",
        text,
    )
    out = re.sub(r"[‌‍\s]+$", "", out)
    # A virama with nothing after it is a glyph sliced in half, not a name.
    # Only the broken cluster goes: "सुरेश ल्" becomes "सुरेश", and a name
    # that merely ends in a conjunct keeps every letter in front of it.
    out = re.sub(r"(?:\s+\S*्[‌‍]*|\S्[‌‍]*)$", "", out)
    return out.strip()


def _drop_lone_tail(text: str) -> str:
    """Drop a one-letter token glued on by the photo edge.

    "छीतर ह" is the husband name "छीतर" plus the first stroke of the photo
    caption. A real name token carries a vowel sign or more than one letter;
    a bare consonant sitting on its own at the end does not.
    """
    parts = text.split()
    if len(parts) < 2:
        return text
    last = parts[-1]
    if len(last) == 1 and "क" <= last <= "ह":
        return " ".join(parts[:-1])
    return text


def _relation_kind_for(label: str) -> Optional[tuple[str, str]]:
    """Map a relation label onto (record suffix, canonical relation word).

    The relation WORD is what belongs in relation_name -- "पति", "पिता",
    "माता", "अन्य" -- matching ocr_pdf_api, which sets relation_name to the
    label's canonical form and puts the person's name in a separate
    voter_<kind>_name. Putting the name in relation_name instead makes the
    field unusable: the consumer cannot tell a husband named "किशन" from a
    father named "किशन", which is the only distinction the field carries.

    Matching is on the label's skeleton, because the matched label is usually
    the full "पिता का नाम" while the bare "पिता" is what is being looked up,
    and OCR may have misread the opening glyph of either.

    The word is returned rather than sliced back out of the label, because a
    label that matched only by fuzzy alignment would otherwise yield a
    misspelling, and this field has to read as one of exactly four values.
    """
    label_skel = _skeleton(label)
    for kind, canonical in (("father", _LABEL_FATHER[0]),
                            ("husband", _LABEL_HUSBAND[0]),
                            ("mother", _LABEL_MOTHER[0]),
                            ("other", _LABEL_OTHER[0])):
        if label_skel.startswith(_skeleton(canonical)):
            return kind, canonical
    return None


def _parse_hindi_card_lines(lines: list[str]) -> dict[str, str]:
    """Split OCR'd card lines into name, relation and gender.

    A card reads as four lines: a photo-caption fragment, then
    "नाम: <name>", "पिता का नाम: <relation>", "मकान संख्या: <n>", and
    "आयु: <n> लिंग: <gender>". Age and gender share a line, so gender is
    read with _field_after_label rather than as a line-leading label.

    Labels are matched at the line's start; a line carrying none is a stray
    fragment and dropped, which is what keeps the caption noise out of the
    name field.

    The relation line yields two separate outputs, and conflating them is the
    single worst bug this parser can have:

        "पति का नाम: किशन"
          relation_name    -> "पति"     (the relation)
          relation_husband -> "किशन"   (the person's name)

    Emitting "किशन" as relation_name loses which relation it was, and the
    consumer has no second field to recover it from, so every record reads as
    an anonymous relative.
    """
    fields: dict[str, str] = {}
    name_parts: list[str] = []
    relation_value = ""
    relation_kind = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        nfc_line = _nfc(stripped)

        found = _label_of_line(nfc_line)
        if found is None:
            # A continuation of the previous value (a wrapped name) is
            # appended rather than dropped, but only while still in the name.
            if name_parts and not relation_value:
                name_parts.append(stripped)
            continue

        label, value_start = found

        if label in (_nfc(_LABEL_HOUSE[0]), _nfc(_LABEL_AGE[0]),
                     _nfc(_LABEL_GENDER[0])):
            # The house/age/gender row shares one line on this template.
            # A deletion stamp often eats the word "लिंग" and leaves the
            # gender itself ("आयु: 58 : पुरूष"), so a missing label is not
            # the same as a missing value.
            if "gender" not in fields:
                gender = _field_after_label(nfc_line, _LABEL_GENDER)
                canonical_gender = _canonical_gender(gender) if gender else ""
                if not canonical_gender:
                    for token in re.findall(r"[ऀ-ॿ]+", nfc_line):
                        canonical_gender = _canonical_gender(token)
                        if canonical_gender:
                            break
                if canonical_gender:
                    fields["gender"] = canonical_gender
            continue

        kind = _relation_kind_for(label)
        if kind is not None:
            relation_kind, relation_word = kind
            # A card carries exactly one relation row. If OCR emits a second
            # one, the first is kept: the later line is far more often a
            # misread of a name that happens to contain a label-shaped prefix
            # than a genuine second relation.
            if not relation_value:
                relation_value = _clean_value(nfc_line[value_start:])
                fields["relation_name"] = relation_word
            continue

        if label == _nfc(_LABEL_NAME[0]):
            name_parts.append(_clean_value(nfc_line[value_start:]))
            continue

    name = _drop_lone_tail(_trim_trailing(" ".join(p for p in name_parts if p)))
    if name:
        fields["voter_name"] = name

    relation_value = _drop_lone_tail(_trim_trailing(relation_value))
    if relation_value:
        fields[f"relation_{relation_kind}"] = relation_value
    return fields


def _find_label_window(line_skel: str, label_skel: str,
                       max_edits: int = 1) -> Optional[tuple[int, int]]:
    """Locate a label anywhere in a line's skeleton.

    Returns (start, end) as offsets into line_skel, or None. Used for the
    labels that share a row -- "आयु: 53 लिंग: पुरूष" carries gender after
    age -- where the label is in the middle of the line rather than at its
    start, so _label_of_line does not apply.
    """
    label_len = len(label_skel)
    if label_len == 0 or not line_skel:
        return None

    best: Optional[tuple[tuple[int, int, int], int, int]] = None
    for start in range(len(line_skel)):
        stop = min(len(line_skel), start + label_len + max_edits)
        for end in range(start + 1, stop + 1):
            distance = _edit_distance_within(
                line_skel[start:end], label_skel, max_edits)
            if distance is None:
                continue
            # Prefer the closest match, then the tightest window, then the
            # leftmost one. Scoring this way keeps a short label from
            # settling on a longer accidental alignment further along the
            # row.
            key = (distance, end - start, start)
            if best is None or key < best[0]:
                best = (key, start, end)

    return None if best is None else (best[1], best[2])


def _field_after_label(line: str, labels: tuple[str, ...]) -> str:
    """Return whatever follows a label inside a single OCR line.

    Matched tolerantly, like _label_of_line: OCR garbles "लिंग" on some cards
    and an exact search would drop the gender for those voters silently.

    The value runs from the end of the matched label to the start of the
    next one on the same line, both located in skeleton space, so the row
    "आयु: 53 लिंग: पुरूष" yields each field separately.
    """
    nfc_line = _nfc(line)
    line_skel = _skeleton(nfc_line)
    positions = _skeleton_positions(nfc_line)
    if not positions:
        return ""

    for label in labels:
        window = _find_label_window(line_skel, _skeleton(label))
        if window is None:
            continue
        _, end = window

        # Where the next label on this line begins, in skeleton offsets.
        stop = len(line_skel)
        for other in _ALL_LABELS:
            if other == label:
                continue
            nxt = _find_label_window(line_skel, _skeleton(other))
            if nxt is not None and nxt[0] >= end and nxt[0] < stop:
                stop = nxt[0]
        if stop < end:
            stop = end

        char_start = positions[end] if end < len(positions) else len(nfc_line)
        char_stop = positions[stop] if stop < len(positions) else len(nfc_line)
        value = _clean_value(nfc_line[char_start:char_stop])
        if value:
            return value
    return ""

# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

# Relation kinds this template prints, mapped onto the sibling service's
# record keys.
_RELATION_KEYS = ("husband", "father", "mother", "other")


def _build_record(
    page: fitz.Page,
    rect: fitz.Rect,
    id_text: str,
    origin_x: float,
    origin_y: float,
    card_index: int,
    page_number: int,
) -> dict[str, Any]:
    """Assemble one voter record from both sources.

    ASCII comes from the text layer, Devanagari from the rendered card, and
    the two are merged into the same internal record shape the sibling
    service emits so that _public_record can be reused unchanged.
    """
    record = _base._empty_record()
    record["id_card_no"] = id_text

    ascii_fields = _ascii_values_from_text_layer(page, origin_x, origin_y)
    record["house_no"] = ascii_fields.get("house_no", "")
    record["age"] = ascii_fields.get("age", "")
    record["sno"] = ascii_fields.get("serial", "")

    hindi_fields, conf = _devanagari_fields_from_render(page, rect)
    record["_tesseract_conf"] = conf

    voter_name = hindi_fields.get("voter_name", "")
    if voter_name:
        first, middle, last = _base._split_person_name(
            _base._canon_deva(voter_name))
        record["voter_first_name"] = first
        record["voter_middle_name"] = middle
        record["voter_sur_name"] = last

    # relation_name is the relation WORD ("पति"/"पिता"/"माता"/"अन्य") and the
    # person's name goes in voter_<kind>_name, which _public_record splits into
    # the first/middle/last triple. The internal record uses the singular
    # unsplit key -- writing the pre-split triple here is why the output had a
    # name in relation_name and empty relation triples at the same time.
    record["relation_name"] = hindi_fields.get("relation_name", "")
    for kind in _RELATION_KEYS:
        value = hindi_fields.get(f"relation_{kind}", "")
        if value:
            record[f"voter_{kind}_name"] = value

    record["gender"] = hindi_fields.get("gender", "")
    # The E/S/R marker printed beside the serial is the roll's own statement
    # that this voter has been struck off. Reading it here is what stops the
    # deleted addendum from being indistinguishable from the live roll.
    marker = ascii_fields.get("deletion_marker", "")
    record["is_deleted"] = bool(marker)
    if marker:
        record["deletion_marker"] = marker
        _base._mark_review(
            record,
            "voter_marked_deleted: %s" % _DELETION_MARKER_MEANING.get(
                marker, "unknown marker"),
        )

    record["_page_number"] = page_number
    record["_card_index"] = card_index
    record["_field_sources"] = {
        "id_card_no": "text_layer",
        "house_no": "text_layer",
        "age": "text_layer",
        "sno": "text_layer",
        "voter_name": "render_ocr",
        "relation_name": "render_ocr",
        "gender": "render_ocr",
    }
    # Only claim a source for the relation when one was actually read. An
    # unconditional "render_ocr" entry asserts the field came from the OCR of
    # the render, which is false for the majority of cards -- most of this
    # template's voters have no relation row at all, and their relation fields
    # are empty rather than read-and-blank.
    for kind in _RELATION_KEYS:
        if hindi_fields.get(f"relation_{kind}"):
            record["_field_sources"][f"voter_{kind}_name"] = "render_ocr"
    return record


def _resolve_page_range(
    page_count: int, start_page: Optional[int], end_page: Optional[int],
    whole_pdf: bool,
) -> tuple[int, int, str]:
    """Resolve the requested page range to a 1-based inclusive (first, last)."""
    if whole_pdf:
        return 1, page_count, "whole_pdf"
    if start_page is None and end_page is None:
        return 1, page_count, "whole_pdf"
    if start_page is None:
        start_page = 1
    if end_page is None:
        end_page = page_count
    first, last = int(start_page), int(end_page)
    if first < 1 or last < first or last > page_count:
        raise ValueError(
            f"Invalid page range {first}..{last} for a {page_count}-page PDF")
    return first, last, "range"


# Header metadata for this template. Municipal rolls declare the state by
# name on page 1 but commonly omit the ``S20``-style ECI token.  Do not bake a
# state into this reader: the source of truth is ECI's active state catalogue.
#
# The list is fetched lazily and cached. That makes the service work for every
# state/UT without issuing an external request for every PDF or page, while a
# temporary ECI outage remains an honest blank/review rather than a guess.
_ECI_STATES_URL = os.getenv(
    "ECI_STATES_URL", "https://gateway-voters.eci.gov.in/api/v1/common/states/")
_ECI_STATES_TIMEOUT_SECONDS = float(os.getenv("ECI_STATES_TIMEOUT_SECONDS", "5"))
_ECI_STATES_CACHE_SECONDS = int(os.getenv("ECI_STATES_CACHE_SECONDS", "86400"))
_ECI_STATES_FAILURE_CACHE_SECONDS = int(
    os.getenv("ECI_STATES_FAILURE_CACHE_SECONDS", "300"))
_ECI_STATES_LOCK = threading.Lock()
_ECI_STATES_CACHE: tuple[float, tuple[dict[str, str], ...], str] | None = None


def _active_eci_states() -> tuple[tuple[dict[str, str], ...], str]:
    """Return ECI's active state/UT catalogue, or an explicit failure reason.

    `stateCd` is returned as ``S20``/``U01`` by ECI.  The extraction contract
    stores its numeric portion because the scanned reader has always emitted
    the number from the printed `S24` token (``24``), so both services retain
    one consistent public field shape.
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
                english = _nfc(str(item.get("stateName") or "")).strip()
                hindi = _nfc(str(item.get("stateNameHindi") or "")).strip()
                if not english and not hindi:
                    continue
                states.append({
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

    This is deliberately exact matching, not fuzzy matching: a wrong state
    code propagates to every voter in a PDF.  Rendered-cover OCR is used as a
    second source when the PDF's embedded font encoding damages Hindi text.
    """
    folded = _nfc(text).casefold()
    matches: list[dict[str, str]] = []
    for state in states:
        for name in (state["english"], state["hindi"]):
            if name and name.casefold() in folded:
                matches.append(state)
                break
    unique = {match["code"]: match for match in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _cover_text_from_render(page: fitz.Page) -> str:
    """OCR the complete page-one render when its embedded Hindi is damaged."""
    try:
        png = page.get_pixmap(dpi=_HEADER_RENDER_DPI, alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return ""
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            return ""
        lines, _confidence = _base._extract_text_with_tesseract(encoded.tobytes())
        return "\n".join(lines)
    except Exception as exc:
        log.warning("Could not OCR text-layer cover for state metadata: %s", exc)
        return ""


def _official_state_from_cover(page: fitz.Page) -> tuple[str, str, str, str]:
    """Resolve state metadata from page one through the live ECI catalogue.

    Returns ``(state_code, state_name, source, review_reason)``.  A blank
    code is intentional when ECI is unavailable or the declared name cannot
    be confidently matched; it is never replaced by a template default.
    """
    states, catalogue_reason = _active_eci_states()
    if not states:
        return "", "", "", catalogue_reason

    text_match = _state_from_text(page.get_text("text"), states)
    if text_match:
        return text_match["code"], text_match["english"], "cover_text", ""

    rendered_match = _state_from_text(_cover_text_from_render(page), states)
    if rendered_match:
        return rendered_match["code"], rendered_match["english"], "cover_render_ocr", ""

    return "", "", "", "state_name_unresolved_from_cover"
#
# Header labels exactly as the text layer stores them. The Devanagari matras
# are destroyed in storage ("सनखयप" for "संख्या"), so these are the corrupted
# spellings, matched on their consonant skeletons -- which survive intact.
#
# THIS PDF USES TWO ENCODINGS. Pages 2-50 embed the header in one font, which
# drops the matras and yields the "_LBL_*" spellings below. Pages 51-62 embed
# it in a second font, which substitutes rather than drops -- "संख्या" becomes
# "ससखजर", "विधान सभा" becomes "नरधरनसभर" -- and the two spellings are more
# than an edit or two apart, so no tolerance that still rejects a mistyped
# header can bridge them. The second encoding is therefore listed as its own
# variant and matched exactly, not widened into.
#
# Loosening the matcher instead would be the tempting fix and the wrong one:
# these labels are long consonant skeletons, so a permissive threshold starts
# accepting a voter's name or a card line as a header, and the numbers parsed
# off it are then written into the roll metadata for the whole document.
_LBL_CONSTITUENCY = "नवधपनसमप कनत कक सनखयप एवन नपम"
_LBL_WARD = "वपरर सनखयप"
_LBL_PART = "मपग सनखयप"

# The second font, as stored. Same three fields, same order.
_LBL_CONSTITUENCY_ALT = "नरधरनसभर ककत कक ससखजर एरस नरम"
_LBL_WARD_ALT = "रररर ससखजर"
_LBL_PART_ALT = "भरग ससखजर"


def _skeleton_matches(line_skel: str, label: str, max_edits: int = 1) -> bool:
    """True if a line's skeleton starts with the label's.

    A prefix test, not equality: the constituency line runs on past its label
    into the value ("... नपम:- 46-झयटवपडप"), while the ward and part lines
    end at theirs. Matching the head of the line covers both.
    """
    label_skel = _skeleton(label)
    if not label_skel or len(line_skel) < len(label_skel):
        return False
    return _edit_distance_within(
        line_skel[:len(label_skel)], label_skel, max_edits) is not None


def _header_number(line: str) -> str:
    """First ASCII number on a header line, if any."""
    match = re.search(r"\d+", line)
    return match.group(0) if match else ""


def _header_name_from_render(page: fitz.Page) -> str:
    """Read the constituency name off the rendered header.

    The text layer spells it as "झयटवपडप"; the drawn glyphs are correct, so
    the name is OCR'd from a render. The crop is anchored to the text
    layer's own bounding box for that line rather than to a fixed fraction
    of the page -- the header sits at y=88..101pt here, well below the
    top-46pt band a fixed guess would have taken, and that band holds the
    commission name instead.
    """
    try:
        # Both fonts, because either one may be the one embedded on this page.
        box = (_header_line_box(page, _LBL_CONSTITUENCY)
               or _header_line_box(page, _LBL_CONSTITUENCY_ALT))
        if box is None:
            return ""
        line_clip = fitz.Rect(page.rect.x0, box[0] - 2.0,
                              page.rect.x1, box[1] + 2.0)
        png = page.get_pixmap(dpi=_HEADER_RENDER_DPI, clip=line_clip,
                              alpha=False).tobytes("png")
        image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return ""

        img_h, img_w = image.shape[:2]
        band = image[:, int(img_w * 0.02):int(img_w * 0.55)]
        if band.size == 0:
            return ""
        ok, enc = cv2.imencode(".png", band)
        if not ok:
            return ""
        lines, _conf = _base._extract_text_with_tesseract(enc.tobytes())
        if not lines:
            return ""
        return _constituency_from_lines(lines)
    except Exception as exc:  # a missing header must not fail extraction
        log.warning("Header name extraction failed: %s", exc)
        return ""


def _header_line_box(page: fitz.Page, label: str) -> Optional[tuple[float, float]]:
    """Vertical extent (y0, y1) in points of the header line carrying label."""
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = _nfc("".join(span.get("text", "")
                                for span in line.get("spans", []))).strip()
            if not text:
                continue
            if _skeleton_matches(_skeleton(text), label):
                bbox = line.get("bbox")
                if bbox:
                    return float(bbox[1]), float(bbox[3])
    return None


def _constituency_from_lines(lines: list[str]) -> str:
    """Pull "<code>-<name>" out of OCR'd header lines."""
    for line in lines:
        text = _nfc(line).strip()
        if not text or _label_end_index(text) < 0:
            continue
        value = _clean_value(text[_label_end_index(text):])
        if not value:
            continue
        match = re.match(r"^(\d+)\s*[-–—.]\s*(.+)$", value)
        if match:
            return _trim_trailing(match.group(2))
        if _skeleton(value):
            return _trim_trailing(value)
    return ""


def _detect_roll_format(page: fitz.Page) -> str:
    """Which roll format this page is printed in: "ward", "assembly", or "".

    A third axis, independent of text-layer-vs-scanned. The two templates do
    not merely differ in how their glyphs are stored -- they print different
    headers and lay their cards out on different grids, so the geometry
    constants below are only valid for one of them.

    The discriminator is the municipal header this template repeats on every
    card page:

        वार्ड संख्या : 28      (ward)
        भाग संख्या : 1        (part)

    An assembly (ECI) roll has no ward and no part -- its booths are numbered
    within a constituency and an anubhag -- so the absence of these two lines
    is what marks it as the other format. Both encodings of the labels are
    accepted, because the same document embeds each header in a different
    font on different pages (see _LBL_WARD and _LBL_WARD_ALT).

    Matching the header rather than the card grid is deliberate. The grid can
    only be measured once the geometry is already assumed, which is circular
    when the point is to decide which geometry applies. The header is
    independent of the layout it sits above.
    """
    for line in page.get_text("text").splitlines():
        nfc = _nfc(line).strip()
        if not nfc or not re.search(r"\d", nfc):
            continue
        skeleton = _skeleton(nfc)
        if (_skeleton_matches(skeleton, _LBL_WARD)
                or _skeleton_matches(skeleton, _LBL_WARD_ALT)):
            return "ward"
    return ""


def _extract_roll_header_metadata(
    page: fitz.Page, *, cover_page: fitz.Page | None = None,
) -> dict[str, str]:
    """Read the roll-wide header printed above the cards.

    This template repeats a five-line header on every card page:

        कार्यालय: नगर निगम / नगर परिषद / नगर पालिका का नाम : जयपुर
        विधान सभा क्षेत्र का सामान्य नाम :- 46-झाटवाड़ा
        वार्ड संख्या : 28
        भाग संख्या : 1

    The numbers are read from the text layer, which stores them exactly as
    printed -- unlike the Devanagari, whose matras the stored layer has
    destroyed ("सनखयप" for "संख्या"), so those are matched on their
    consonant skeletons. The constituency name is read from the rendered
    header instead, since the text layer cannot be trusted to spell it.
    """
    state_code, state_name, state_source, state_review_reason = (
        _official_state_from_cover(cover_page) if cover_page else (
            "", "", "", "state_cover_page_not_supplied"))
    meta = {
        "state_code": state_code,
        # These audit fields are intentionally returned with roll metadata.
        # The pipeline persists state_code; callers can inspect exactly how it
        # was obtained rather than mistaking a lookup/default for OCR output.
        "state_name": state_name,
        "state_resolution_source": state_source,
        "state_review_reason": state_review_reason,
        "roll_format": "municipal_ward",
        "ward_code": "",
        # A municipal ward roll has a ward and a part, but no assembly-roll
        # anubhag.  Keep those concepts separate: callers can show the ward
        # instead of mistaking an honest blank for an extraction failure.
        "anubhag_status": "not_applicable",
        "anubhag_review_reason": "municipal_ward_roll_has_no_assembly_anubhag",
        "ac_code": "",
        "anubhag_code": "",
        "anubhag_name": "",
        "booth_code": "",
    }

    # --- text layer: the numeric fields, exact -----------------------------
    ac_code = ""
    anubhag_name = ""
    ward = ""
    part = ""
    for line in page.get_text("text").splitlines():
        nfc = _nfc(line).strip()
        if not nfc:
            continue
        skeleton = _skeleton(nfc)
        if not re.search(r"\d", nfc):
            continue

        if _skeleton_matches(skeleton, _LBL_WARD) and not ward:
            ward = _header_number(nfc)
        elif _skeleton_matches(skeleton, _LBL_PART) and not part:
            part = _header_number(nfc)
        elif _skeleton_matches(skeleton, _LBL_CONSTITUENCY) and not ac_code:
            # "विधान सभा क्षेत्र का सामान्य नाम :- 46-झाटवाड़ा" carries the
            # assembly constituency number before the dash and its name after.
            match = re.search(r"(\d+)\s*[-–—.]\s*(.*)$", nfc)
            if match:
                ac_code = match.group(1)
                if match.group(2).strip():
                    anubhag_name = match.group(2).strip()
        elif _skeleton_matches(skeleton, _LBL_PART_ALT) and not part:
            part = _header_number(nfc)
        elif _skeleton_matches(skeleton, _LBL_WARD_ALT) and not ward:
            ward = _header_number(nfc)
        elif _skeleton_matches(skeleton, _LBL_CONSTITUENCY_ALT) and not ac_code:
            # The second font stores the same line; the number and the name
            # are still ASCII digits and Devanagari respectively, so the parse
            # is identical. Only the label's spelling differs.
            match = re.search(r"(\d+)\s*[-–—.]\s*(.*)$", nfc)
            if match:
                ac_code = match.group(1)
                if match.group(2).strip():
                    anubhag_name = match.group(2).strip()

    # ac_code is the assembly constituency ("46"). booth_code is the part
    # number ALONE -- "भाग संख्या : 1" is the booth's own identity within the
    # roll. The ward is not part of it: the ward is a different administrative
    # unit that happens to be numbered, and folding it in ("28-1") produced a
    # code that matches no roll anywhere. The ward has no field of its own in
    # the output schema, so it is read to validate the header and then
    # dropped rather than invented into an existing one.
    #
    # Anubhag is an assembly-roll concept this template has no field for, so
    # it stays empty rather than being guessed at.
    meta["ac_code"] = ac_code
    meta["booth_code"] = part
    meta["ward_code"] = ward

    # --- rendered header: the constituency name, spelled correctly ---------
    # The render wins over the text layer, which stores this name corrupted
    # ("झयटवपडप" for "झाटवाड़ा"). It is consulted for the name only: that is
    # the one header field whose spelling matters downstream, and OCR still
    # mis-reads a matra occasionally ("झोटवाड़ा"), so the two spellings are
    # cross-checked and a disagreement is logged rather than hidden.
    rendered = _header_name_from_render(page)
    meta["anubhag_name"] = rendered or anubhag_name

    if rendered and anubhag_name:
        agree = _edit_distance_within(
            _skeleton(rendered), _skeleton(anubhag_name),
            _HEADER_NAME_MAX_EDITS) is not None
        if not agree:
            log.warning(
                "Header constituency name differs between text layer and "
                "render: %r vs %r -- using the render",
                anubhag_name, rendered)

    return meta


def _extract_pdf_text_unlocked(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int],
    end_page: Optional[int],
    whole_pdf: bool,
    skip_non_voter_pages: bool,
) -> dict[str, Any]:
    """Extract voter records from a born-digital roll. Stateless."""
    started = time.perf_counter()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc

    try:
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")

        # Fail loudly on the wrong template, before doing any work.
        kind, evidence = _detect_pdf_kind(doc)
        if kind == "scanned":
            raise UnsupportedPdf(
                "This PDF has no text layer, so it is an image-scanned roll. "
                "Use ocr_pdf_api on port 8082 for scanned rolls. "
                f"(sampled {evidence['pages_sampled']} pages, "
                f"{evidence['total_text_chars']} text chars)"
            )

        page_count = doc.page_count
        first, last, mode = _resolve_page_range(
            page_count, start_page, end_page, whole_pdf)

        _progress_reset(pages_total=last - first + 1)

        records: list[dict[str, Any]] = []
        processed_pages: list[dict[str, Any]] = []
        skipped_pages: list[dict[str, Any]] = []

        # A unit can start on any voter page. State declaration is on page 1,
        # so pass the cover explicitly instead of binding metadata to the
        # current unit's first page.
        roll_metadata = _extract_roll_header_metadata(
            doc[first - 1], cover_page=doc[0])

        for page_number in range(first, last + 1):
            page_started = time.perf_counter()
            page = doc[page_number - 1]
            cards = _cards_on_page(page)

            if not cards:
                if skip_non_voter_pages:
                    skipped_pages.append({
                        "page_number": page_number,
                        "reason": "no voter-card grid recognised on this page",
                    })
                    continue
                cards = []

            # Cards are ordered top-to-bottom then left-to-right by
            # _card_anchors, which is the order serials run in, so the
            # running card index doubles as a stable identity even when the
            # serial itself is unreadable.
            page_records: list[dict[str, Any]] = []
            for card_index, (rect, id_text, origin_x, origin_y) in enumerate(cards):
                record = _build_record(
                    page, rect, id_text, origin_x, origin_y,
                    card_index, page_number,
                )
                _base._review_record(record)
                if roll_metadata["state_review_reason"]:
                    _base._mark_review(
                        record, roll_metadata["state_review_reason"])
                page_records.append(record)

            _flag_broken_serial_run(page_records, records)

            records.extend(page_records)
            processed_pages.append({
                "page_number": page_number,
                "records": len(page_records),
            })
            _progress_page_done(page_number, len(cards))
            _progress(
                f"page {page_number} complete cards={len(cards)} "
                f"records={len(page_records)} "
                f"in {time.perf_counter() - page_started:.2f}s",
                current_page=page_number,
                records=len(records),
            )

        source_pdf_name = _source_pdf_name(filename)
        public_records = [
            _base._public_record(record, counter, source_pdf_name, roll_metadata)
            for counter, record in enumerate(records, 1)
        ]
        review_reason_counts = Counter(
            reason
            for record in public_records
            for reason in record["review_reasons"]
        )

        _progress_finish()

        elapsed = time.perf_counter() - started
        print(
            f"[OCR-TEXT] request_complete pages={len(processed_pages)} "
            f"skipped={len(skipped_pages)} records={len(public_records)} "
            f"elapsed={elapsed:.2f}s",
            flush=True,
        )

        return {
            "ok": True,
            "filename": source_pdf_name,
            "page_count": page_count,
            "pdf_kind": kind,
            "template_evidence": evidence,
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
            "serial_corrections_count": 0,
            "serial_correction_details": [],
            "records_count": len(public_records),
            "records_needing_review": sum(
                record["needs_review"] for record in public_records),
            "review_reason_counts": dict(sorted(review_reason_counts.items())),
            "elapsed_seconds": round(elapsed, 2),
            "records": public_records,
        }
    finally:
        doc.close()


def extract_pdf_text(
    pdf_bytes: bytes,
    filename: str,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    whole_pdf: bool = False,
    skip_non_voter_pages: bool = True,
) -> dict[str, Any]:
    """Extract from a born-digital roll. No file lock, stateless."""
    return _extract_pdf_text_unlocked(
        pdf_bytes, filename, start_page, end_page, whole_pdf,
        skip_non_voter_pages,
    )


def _safe_filename(filename: str, default: str = "document.pdf") -> str:
    return Path(filename or default).name or default


def _source_pdf_name(filename: str) -> str:
    return _safe_filename(filename)


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    """Write JSON to file atomically via a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

OCR_OUTPUT_DIR = Path(
    os.getenv("OCR_OUTPUT_DIR",
               str(Path(__file__).parent.parent / "results"))
).expanduser().resolve()

app = FastAPI(
    title="Electoral Roll OCR (Text-Layer PDFs)",
    description=(
        "Extracts voter records from born-digital electoral-roll PDFs. "
        "For image-scanned rolls use ocr_pdf_api on port 8082."
    ),
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ocr_text_pdf_api",
        "handles": "born-digital (text-layer) electoral rolls",
        "scanned_rolls_service": "ocr_pdf_api on port 8082",
        "template": "Jaipur-style municipal roll (3 columns, measured geometry)",
    }


@app.get("/ocr/progress")
async def ocr_progress() -> dict[str, Any]:
    return _progress_snapshot()


@app.post("/ocr/extract")
async def ocr_extract_endpoint(
    pdf_file: UploadFile = File(...),
    start_page: Optional[int] = Form(None),
    end_page: Optional[int] = Form(None),
    whole_pdf: bool = Form(False),
    skip_non_voter_pages: bool = Form(True),
) -> Response:
    filename = pdf_file.filename or "document.pdf"
    content = await pdf_file.read()
    try:
        result = extract_pdf_text(
            content,
            filename,
            start_page,
            end_page,
            whole_pdf,
            skip_non_voter_pages,
        )
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_",
                           Path(filename).stem).strip(" .") or "document"
        output_path = OCR_OUTPUT_DIR / f"{uuid.uuid4().hex}_{safe_stem}_ocr.json"
        result["json_output_file"] = output_path.name
        await asyncio.to_thread(_write_json_atomically, output_path, result)
        return Response(
            content=json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
        )
    except UnsupportedPdf as exc:
        # 422, not 500: the request was well-formed but this service cannot
        # serve it, and the message says which service can.
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        log.exception("Text-layer OCR extraction failed")
        raise HTTPException(500, f"OCR extraction failed: {exc}") from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("OCR_API_HOST", "0.0.0.0"),
        port=int(os.getenv("OCR_TEXT_API_PORT", "8083")),
    )
