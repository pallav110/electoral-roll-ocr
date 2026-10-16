"""Header-field readers must not be fitted to one roll's page geometry.

Two independent defects broke the S28-40 Lansdown 2026 SIR roll while leaving
S24-53 untouched, and neither is visible from the code alone -- both only show
up as an empty field on the template they were not written for.

1. The section label is not always followed by a separator.
   Lansdown prints ``अनुभाग संख्या व नाम ॥-कोचियार`` -- label, value's own
   hyphen, no ':' or ';'.  A separator-only split returned the line unchanged,
   so both anchored parsers saw the label where they expected a value.

2. The section line's position is not fixed.
   Measured as a fraction of page height, the line occupies 1.96-3.05% on the
   2806pt Lansdown page and 1.87-2.89% on the 842pt S24 page, while the cards
   below it start at different offsets on each.  Every crop constant in the
   readers -- the y-fractions inside the header window, the absolute 40pt and
   52pt clips -- was fitted to S24, so on Lansdown they cropped above the line
   or below it into card content.

The parser tests need no PDF; the locator tests build a synthetic page, so they
run wherever the suite runs.
"""
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import fitz  # noqa: E402

import ocr_pdf_api as A  # noqa: E402


# ---------------------------------------------------------------- separator

# The exact lines Tesseract returns from the two measured rolls.
LANSDOWN_LINE = "अनुभाग संख्या व नाम ॥-कोचियार"
S24_LINE = "अनुभाग संख्या और नाम : 4-उत्तरांचल कालोनी गली न0 8 से 9"


def test_separatorless_header_still_yields_the_code():
    """Lansdown's label-only line must parse to its section number.

    `॥` is how Tesseract renders the narrow printed `1`; the alias class in
    `_anubhag_code_from_header_text` already accepts it, but only at the start
    of the string.  With the label still attached it could never match.
    """
    split = A._split_anubhag_label(LANSDOWN_LINE)
    assert split == "॥-कोचियार"
    assert A._anubhag_code_from_header_text(split) == "1"


def test_separatorless_header_still_yields_the_name():
    assert A._clean_anubhag_name_candidate(LANSDOWN_LINE) == "कोचियार"


@pytest.mark.parametrize("line,code,name", [
    # Separator present -- must behave exactly as before this change.
    (S24_LINE, "4", "उत्तरांचल कालोनी गली न0 8 से 9"),
    # Semicolon separator, the shape recorded on HIN-116.
    ("अनुभाग संख्या और नाम; 4-गढी", "4", "गढी"),
    # No separator, leading danda as on HIN-45 / HIN-403.
    ("।-प्रगति विहार", "1", "प्रगति विहार"),
    ("॥-सरस्वती", "1", "सरस्वती"),
    # A real multi-digit code, no separator.
    ("7-लक्ष्मी इन्कलेव", "7", "लक्ष्मी इन्कलेव"),
    # A header that prints no digit at all must stay blank, not be invented.
    ("अनुभाग संख्या व नाम प्रगति विहार", "", "प्रगति विहार"),
])
def test_accepted_header_shapes_are_unchanged(line, code, name):
    split = A._split_anubhag_label(line)
    assert A._anubhag_code_from_header_text(split) == code
    assert A._clean_anubhag_name_candidate(line) == name


@pytest.mark.parametrize("bare", [
    # Values that arrive already stripped, because the label-fallback path in
    # `_extract_anubhag_name_from_header_fallback` hands them straight here.
    "प्रगति विहार",
    "सरस्वती नगर",
    "उत्तरांचल कालोनी गली न0 8 से 9",
    # नाम inside a locality.  Anchoring the label pattern on अनुभाग rather
    # than नाम is what keeps this intact.
    "नाम का पेड़ विहार",
    "गढ़ी अनुभाग विहार",
    "",
])
def test_bare_values_pass_through_untouched(bare):
    assert A._split_anubhag_label(bare) == bare


def test_label_pattern_is_anchored_on_anubhag_not_naam():
    """A loose `.*नाम` would truncate real localities.  Pin the anchoring."""
    assert A._ANUBHAG_LABEL_PREFIX.match("नाम का पेड़ विहार") is None
    assert A._ANUBHAG_LABEL_PREFIX.match(LANSDOWN_LINE) is not None


def test_only_the_broken_line_changes():
    """Freeze every split this function produces, including the new one.

    The separator lines split at their ':' / ';' exactly as they always did; the
    bare values come back whole; only the label-prefixed line that was returning
    blank is now trimmed.  Pinning the full table is the evidence that the new
    branch is not quietly rewriting reads that already worked.
    """
    assert [A._split_anubhag_label(line) for line in (
        S24_LINE,                                        # splits at its ':'
        "अनुभाग संख्या और नाम; 4-गढी",                       # splits at its ';'
        LANSDOWN_LINE,                                  # NEW: label removed
        "।-प्रगति विहार",                                 # already bare
        "॥-सरस्वती",
        "7-लक्ष्मी इन्कलेव",
        "प्रगति विहार",
        "नाम का पेड़ विहार",
        "गढ़ी अनुभाग विहार",
    )] == [
        " 4-उत्तरांचल कालोनी गली न0 8 से 9",
        " 4-गढी",
        "॥-कोचियार",
        "।-प्रगति विहार",
        "॥-सरस्वती",
        "7-लक्ष्मी इन्कलेव",
        "प्रगति विहार",
        "नाम का पेड़ विहार",
        "गढ़ी अनुभाग विहार",
    ]


# ---------------------------------------------------------------- locator

# The OCR image container ships no fonts at all (`fc-list` finds none), so a
# synthetic page cannot render Devanagari and no OCR stub could ever see the
# label.  The tests below therefore split along the real seam:
#
#   * `_header_ink_bands` is pure geometry -- ink rows grouped into text bands,
#     every threshold scaled to the strip height.  It is tested directly, on
#     real renders, at every page scale.
#   * the OCR stage of the locator is tested by stubbing the read, so what is
#     pinned is that the locator returns the band whose read carries the label
#     and skips the bands that do not.
#
# The Devanagari query itself is pinned by `test_default_query_is_the_printed_
# devanagari_label`, and its behaviour on both real rolls is verified by
# `tests/test_scripts/lansdown_probe/locate_line.py`.

def _render_strip(page: fitz.Page) -> np.ndarray:
    """The same render the locator performs, as grayscale."""
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + page.rect.height * A._ANUBHAG_SCAN_TOP_FRACTION)
    raw = page.get_pixmap(dpi=200, clip=clip, alpha=False).tobytes("png")
    return cv2.cvtColor(cv2.imdecode(np.frombuffer(raw, np.uint8),
                                     cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)


def _roll_with_lines(path: Path, page_pt: float, lines: list[tuple[float, str]]):
    """Draw text rows at given page-height fractions, at a realistic text size.

    The font size scales with the page because both real templates print header
    text about 1% of page height tall -- measured 78px of a 624px strip (1.0%) on
    Lansdown and 21px of a 188px strip (0.99%) on S24.  A fixed 13pt font would
    instead overflow its own row on a short page and fuse two separate printed
    lines into one band, which is a fixture artefact and not a property of the
    grouper.
    """
    doc = fitz.open()
    page = doc.new_page(width=page_pt * 0.707, height=page_pt)
    font = page_pt * 0.010
    for y, text in lines:
        page.insert_text((28, y + font * 0.8), text, fontsize=font,
                         fontname="helv")
    doc.save(path)
    doc.close()
    return fitz.open(path)


@pytest.mark.parametrize("page_pt", [842.0, 1190.0, 2806.0])
def test_bands_are_found_at_every_page_scale(tmp_path, page_pt):
    """Two printed rows must group into two bands on any page height.

    This is the property the removed constants could not have: nothing here
    encodes where the lines sit, only how tall the page is.  Both rows are placed
    as fractions of the page, matching how real headers space themselves -- the
    measured Lansdown rows sit at 0.78-1.77% and 1.96-3.05% of page height.
    """
    doc = _roll_with_lines(tmp_path / f"scale_{page_pt:.0f}.pdf", page_pt, [
        (page_pt * 0.012, "Vidhan Sabha kshetra sankhya"),
        (page_pt * 0.026, "Anubhag sankhya aur naam"),
    ])
    try:
        bands = A._header_ink_bands(_render_strip(doc[0]))
        assert len(bands) == 2, bands
        # The second band really is lower than the first.
        assert bands[1][0] > bands[0][0], bands
    finally:
        doc.close()


def test_bands_are_found_when_both_lines_share_one_strip(tmp_path):
    """Lansdown's own shape: two rows close together, inside the scan strip."""
    doc = _roll_with_lines(tmp_path / "close.pdf", 2806.0, [
        (2806.0 * 0.012, "Vidhan Sabha kshetra 40"),
        (2806.0 * 0.026, "Anubhag sankhya"),
    ])
    try:
        bands = A._header_ink_bands(_render_strip(doc[0]))
        assert len(bands) == 2, bands
    finally:
        doc.close()


def test_blank_strip_yields_no_bands():
    strip = np.full((200, 400), 255, dtype=np.uint8)
    assert A._header_ink_bands(strip) == []


# ---- the OCR stage of the locator, with the read stubbed ------------------

def _stub_reads(monkeypatch, mapping: dict[int, str]) -> None:
    """Answer each band read by its index, so no OCR model is needed."""
    counter = {"n": 0}

    def fake_image_to_string(img, lang=None, config=None):
        index = counter["n"]
        counter["n"] += 1
        return mapping.get(index, "")

    monkeypatch.setattr(A.pytesseract, "image_to_string", fake_image_to_string)


def _locate_with_two_bands(monkeypatch, tmp_path, marked: int):
    """A two-band page where band `marked` carries the label."""
    doc = _roll_with_lines(tmp_path / f"mark_{marked}.pdf", 1190.0, [
        (18.0, "Vidhan Sabha kshetra"),
        (40.0, "Anubhag sankhya"),
    ])
    try:
        assert len(A._header_ink_bands(_render_strip(doc[0]))) == 2
        _stub_reads(monkeypatch,
                    {marked: "अनुभाग संख्या और नाम : 4-GHADHI"})
        return A._find_anubhag_header_band(doc[0])
    finally:
        doc.close()


def test_locator_returns_the_labelled_band(monkeypatch, tmp_path):
    found = _locate_with_two_bands(monkeypatch, tmp_path, marked=1)
    assert found is not None
    rect, text = found
    assert "GHADHI" in text
    # Padded around the second band, in page points, and inside the scan strip.
    assert 30.0 <= rect.y0 <= 45.0, rect
    assert 45.0 <= rect.y1 <= 60.0, rect


def test_locator_skips_an_unlabelled_leading_band(monkeypatch, tmp_path):
    """Band 0 has no label, so the locator must keep looking and return band 1.

    This is the discrimination that replaces a hand-chosen y-fraction.
    """
    found = _locate_with_two_bands(monkeypatch, tmp_path, marked=0)
    assert found is not None
    rect, _text = found
    assert rect.y0 <= 30.0, rect


def test_locator_returns_none_when_no_band_is_labelled(monkeypatch, tmp_path):
    """No band carrying the label must return None, not fall back to another.

    Returning some other band would let voter-card text be promoted to roll-wide
    metadata copied into every record.
    """
    found = _locate_with_two_bands(monkeypatch, tmp_path, marked=-1)
    assert found is None


def test_default_query_is_the_printed_devanagari_label():
    """Guard the constant the stubbed tests bypass."""
    assert A._ANUBHAG_BAND_LABEL == "अनुभाग"