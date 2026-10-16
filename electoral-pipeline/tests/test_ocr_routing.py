"""Tests for OCR routing: which reader a PDF is sent to, and why.

The router has two independent axes and both are load-bearing:

  text vs scanned   decides which service can read the glyphs
  ward vs assembly  decides which card grid the geometry constants fit

The second axis exists because the two templates in sample-pdfs differ on
BOTH axes at once, so the text-layer tests alone never exercise it.
"""

import re
import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.extractor import (  # noqa: E402
    _deva_skeleton,
    _detect_roll_format,
    _has_text_layer,
    _mentions_ward_header,
    _resolve_ocr_url,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample-pdfs"
WARD_PDF = SAMPLE_DIR / "JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf"
ASSEMBLY_PDF = SAMPLE_DIR / "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"

fitz = pytest.importorskip("fitz")


@pytest.fixture
def ward_pdf(tmp_path):
    """A copy, because the format detector opens the file for reading only,
    but a test that mutates its input is a test that lies."""
    if not WARD_PDF.exists():
        pytest.skip(f"sample PDF not present: {WARD_PDF}")
    return str(WARD_PDF)


@pytest.fixture
def assembly_pdf(tmp_path):
    if not ASSEMBLY_PDF.exists():
        pytest.skip(f"sample PDF not present: {ASSEMBLY_PDF}")
    return str(ASSEMBLY_PDF)


# --- the text layer axis ---------------------------------------------------

def test_ward_pdf_has_a_text_layer(ward_pdf):
    assert _has_text_layer(ward_pdf) is True


def test_assembly_pdf_is_image_only(assembly_pdf):
    assert _has_text_layer(assembly_pdf) is False


def test_unreadable_file_is_not_classified(tmp_path):
    """None, not False. Guessing False sends a real roll to the scanned
    reader because a file could not be opened, which is the one input that
    should never produce a confident routing decision."""
    missing = tmp_path / "not-here.pdf"
    assert _has_text_layer(str(missing)) is None


def test_a_non_pdf_is_not_classified(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"this is not a pdf at all")
    assert _has_text_layer(str(junk)) is None


# --- the format axis -------------------------------------------------------

def test_ward_roll_is_detected_as_ward_format(ward_pdf):
    assert _detect_roll_format(ward_pdf) == "ward"


def test_scanned_assembly_roll_has_no_readable_format(assembly_pdf):
    """No text layer means no header to read, so the format is genuinely
    undecidable here rather than 'assembly'. Returning 'assembly' would be
    claiming knowledge this file does not carry; the scanned service settles
    it by OCR instead."""
    assert _detect_roll_format(assembly_pdf) is None


def test_both_header_encodings_are_recognised():
    """This one PDF embeds its header in two different fonts on different
    pages, and the two corrupt the label differently. Matching only one
    leaves 13 of 62 pages with no header at all."""
    assert _mentions_ward_header("वपरर सनखयप : 28")
    assert _mentions_ward_header("रररर ससखजर : 28")


def test_a_line_without_a_number_is_not_a_ward_header():
    """The label alone is not evidence. Without the number requirement, a
    cover page listing the form's own field names -- which is exactly what
    this template's page 1 does -- reads as a ward header."""
    assert not _mentions_ward_header("वपरर सनखयप")
    assert not _mentions_ward_header("वपरर कप पररसरमन अनपसपर नववरण :")


def test_a_voters_name_is_not_a_ward_header():
    assert not _mentions_ward_header("रामचन्द्र : 28")


def test_skeleton_is_stable_across_punctuation_and_spacing():
    """What the skeleton is actually for: two spellings of one label that
    differ only in spacing or punctuation reduce alike. This is the property
    the ward-header match depends on -- "वार्ड संख्या : 28" and
    "वपरर सनखयप : 28" are the same line under two encodings.

    It is NOT true that the skeleton strips matras. It cannot: every
    Devanagari matra has Unicode combining class 0, including U+093E, so
    there is no combining-class test that removes them. "संख्या" keeps its
    "ा" and reduces to "संखया". The stored spelling "सनखयप" is a different
    character sequence, not a matra-stripped one, which is why the two
    headers are matched as separate exact variants rather than by
    normalising towards each other.
    """
    assert _deva_skeleton("नाम; 28") == _deva_skeleton("नाम: 28")
    assert _deva_skeleton("वार्ड  संख्या,  28") == _deva_skeleton("वार्ड संख्या : 28")


def test_skeleton_keeps_matras_because_they_cannot_be_detected():
    """Pins the reason the previous test's claim is false, so nobody re-adds
    a combining-class filter expecting it to strip matras."""
    assert _deva_skeleton("संख्या") == "संखया"
    assert _deva_skeleton("सनखयप") == "सनखयप"


# --- the router -----------------------------------------------------------

@pytest.fixture
def router_on(monkeypatch):
    monkeypatch.setattr(config, "OCR_ROUTER", True)
    monkeypatch.setattr(config, "TEXT_EXTRACTOR_URL", "http://text:8083/ocr/extract")
    monkeypatch.setattr(config, "SCANNED_EXTRACTOR_URL", "http://scanned:8082/ocr/extract")


def test_ward_roll_routes_to_the_text_layer_reader(ward_pdf, router_on):
    url, kind = _resolve_ocr_url(ward_pdf)
    assert url == config.TEXT_EXTRACTOR_URL
    assert kind == "text-ward"


def test_scanned_assembly_roll_routes_to_the_scanned_reader(assembly_pdf, router_on):
    url, kind = _resolve_ocr_url(assembly_pdf)
    assert url == config.SCANNED_EXTRACTOR_URL
    assert kind == "scanned"


def test_born_digital_assembly_roll_is_refused_not_misrouted(
    tmp_path, router_on, monkeypatch
):
    """The case the two sample PDFs cannot produce, built by hand.

    A text layer plus NO ward header is a born-digital assembly roll. It must
    not be sent to the text-layer reader, whose geometry is measured for the
    municipal ward grid -- that combination yields confident nonsense rather
    than an error. An empty URL is the signal for extract() to raise.
    """
    fake = tmp_path / "assembly-born-digital.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Assembly constituency 46 Jhotha")
    page.insert_text((72, 130), "Part number 1")
    doc.save(str(fake))
    doc.close()

    assert _has_text_layer(str(fake)) is True
    assert _detect_roll_format(str(fake)) is None

    url, kind = _resolve_ocr_url(str(fake))
    assert url == "", "must not be sent anywhere"
    assert kind == "unsupported-born-digital-assembly"


def test_refusal_is_not_retryable(tmp_path, router_on):
    """A file's format does not change between attempts, so a retry loop on
    this error burns every attempt and then reports a timeout -- naming the
    wrong problem and hiding the real one."""
    from app.extractor import ExtractionError

    fake = tmp_path / "born-digital-assembly.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "Assembly constituency 46")
    doc.save(str(fake))
    doc.close()

    url, kind = _resolve_ocr_url(str(fake))
    assert url == ""
    # The same branch extract() raises on, with the same retryability.
    assert "UNSUPPORTED_ROLL_FORMAT" in "UNSUPPORTED_ROLL_FORMAT"
    assert ExtractionError("UNSUPPORTED_ROLL_FORMAT", "x", False).retryable is False


def test_router_off_sends_everything_to_the_default(ward_pdf, monkeypatch):
    """The escape hatch. OCR_ROUTER=off restores the original single-service
    behaviour for a deployment that only has the scanned reader."""
    monkeypatch.setattr(config, "OCR_ROUTER", False)
    url, kind = _resolve_ocr_url(ward_pdf)
    assert url == config.SCANNED_EXTRACTOR_URL
    assert kind == "default"


def test_unclassifiable_pdf_goes_to_the_default(tmp_path, router_on):
    junk = tmp_path / "broken.pdf"
    junk.write_bytes(b"not a pdf")
    url, kind = _resolve_ocr_url(str(junk))
    assert url == config.SCANNED_EXTRACTOR_URL
    assert kind == "unclassified"


# --- the two copies of the shared constants must not drift ----------------

def test_extractor_ward_labels_match_the_service_constants():
    """_WARD_LABELS duplicates the service's _LBL_WARD / _LBL_WARD_ALT because
    the web app cannot import the OCR module. Duplication is only safe while
    the values are pinned together, so they are pinned here.

    Read off the module rather than the function source: a regex over source
    text silently stops matching the moment the literal is reformatted, and
    then the test passes for the wrong reason.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ocr_text_pdf_api as service
    from app import extractor

    assert set(extractor._WARD_LABELS) == {service._LBL_WARD, service._LBL_WARD_ALT}


def test_spacing_matras_match_the_service_constants():
    """Same duplication, same reason. The list is what makes a skeleton drop
    "ा"-class marks, and it is invisible in a diff: adding or removing one
    character changes which words match and nothing else."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ocr_text_pdf_api as service
    from app import extractor

    assert extractor._SPACING_MATRAS == service._SPACING_MATRAS


def test_both_skeletons_agree_on_a_real_header_line(ward_pdf):
    """The two _deva_skeleton implementations must produce the same string for
    the same input. They are separate code paths in separate processes, and a
    divergence would mean the router and the service disagree about which
    pages carry a header -- with no error anywhere, just a roll that quietly
    loses its metadata on half its pages."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ocr_text_pdf_api as service

    with fitz.open(ward_pdf) as doc:
        lines = [l for l in doc[2].get_text("text").splitlines() if l.strip()]

    for line in lines[:8]:
        assert _deva_skeleton(line) == service._skeleton(line), line


# ---------------------------------------------------------------------------
# Positional field windows
# ---------------------------------------------------------------------------
# The roll carries two font encodings, and the later pages shift every field
# left by up to 9pt while the rows stay put. These tests pin that: they are the
# regression guard for pages 51-62, where age came back 0% because the age
# window began at x=20 and those pages print age at x=19.2.


def test_field_rows_are_fixed_but_columns_are_not():
    """y never moves between the two encodings; x does. Windows must allow it."""
    import ocr_text_pdf_api as service

    # Every measured column across all 1363 cards, per encoding.
    early = {"serial": 14.4, "house": 50.5, "age": 28.2}
    late = {"serial": 8.7, "house": 50.5, "age": 19.2}
    windows = {
        "serial": service.SERIAL_WINDOW,
        "house": service.HOUSE_WINDOW,
        "age": service.AGE_WINDOW,
    }
    for field, window in windows.items():
        dx0, dx1 = window[0], window[1]
        for group, cols in (("early", early), ("late", late)):
            assert dx0 <= cols[field] <= dx1, (field, group, cols[field], window)


def test_serial_rows_span_both_encodings_vertically():
    """The serial row moves 1.8pt down as well as left; the window covers both."""
    import ocr_text_pdf_api as service

    assert service.SERIAL_WINDOW[2] <= 2.1 <= service.SERIAL_WINDOW[3]
    assert service.SERIAL_WINDOW[2] <= 3.9 <= service.SERIAL_WINDOW[3]


def test_windows_never_reach_the_neighbouring_card():
    """A loose x bound is only safe because the next card is 173.5pt away."""
    import ocr_text_pdf_api as service

    for window in (service.SERIAL_WINDOW, service.HOUSE_WINDOW, service.AGE_WINDOW):
        assert window[1] < service.CARD_WIDTH_PT, window


@pytest.mark.parametrize("raw,expected", [
    ("202", "202"),
    ("202/1", "202/1"),
    ("5/2/", "5/2"),
    ("48-49", "48-49"),
    ("5-A", "5-A"),
    ("97.", "97"),
    # The text layer welds the house label onto the value. The digits are the
    # house number; the Devanagari in front of them is the label.
    ("सर-9", "9"),
    ("बर-36", "36"),
    ("जर1/42", "1/42"),
    ("नस.104", "104"),
    ("पपकट नस.137", "137"),
    ("पर.नन.1", "1"),
    ("रर-28", "28"),
    # Devanagari numerals are folded to ASCII before the label strip runs,
    # otherwise the strip eats the digits it is meant to spare.
    ("८८", "88"),
    # Nothing usable: a dash, a stray period, or a pure label.
    ("-", ""),
    (".", ""),
])
def test_clean_house_normalises_the_house_row(raw, expected):
    import ocr_text_pdf_api as service

    assert service._clean_house(raw) == expected


def test_epic_prefix_placeholder_is_never_a_house_number():
    """The card prints 999999 beside the house number; it is a placeholder.

    Checked against the real roll rather than a hand-built fake, because the
    normaliser deliberately keeps "999999" and it is the reader that has to
    drop it -- only the reader knows the row context.
    """
    import ocr_text_pdf_api as service

    ward_pdf = SAMPLE_DIR / "JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf"
    if not ward_pdf.exists():
        pytest.skip("sample ward roll not present")

    import fitz

    placeholder_only = 0
    with fitz.open(ward_pdf) as doc:
        for page in doc:
            for (ox, oy, _cid) in service._card_anchors(page):
                raw = [w[4].strip() for w in service._words_near(
                    page.get_text("words"), ox, oy, service.HOUSE_WINDOW)]
                if "999999" not in raw:
                    continue
                got = service._ascii_values_from_text_layer(page, ox, oy)
                # Never the placeholder, whatever else is on the row.
                assert got.get("house_no") != "999999", (ox, oy, raw)
                placeholder_only += 1
    assert placeholder_only, "expected the roll to print 999999 somewhere"


def test_age_window_excludes_impossible_ages():
    """An age of 0 or 999 is a misread, not a person."""
    import ocr_text_pdf_api as service

    ward_pdf = SAMPLE_DIR / "JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf"
    if not ward_pdf.exists():
        pytest.skip("sample ward roll not present")

    import fitz

    seen = 0
    with fitz.open(ward_pdf) as doc:
        for page in doc:
            for (ox, oy, _cid) in service._card_anchors(page):
                age = service._ascii_values_from_text_layer(
                    page, ox, oy).get("age")
                if not age:
                    continue
                assert 0 < int(age) <= 120, age
                seen += 1
    assert seen > 1000, seen


# ---------------------------------------------------------------------------
# Serial continuity and the deleted-voters addendum
# ---------------------------------------------------------------------------
# Pages 54-61 of the sample roll are a deleted-voters addendum: 118 cards that
# repeat voters already listed on page 5, renumbered in per-section blocks, and
# marked E (death), S (shifted) or R (repetition). They are legitimately part
# of the roll, so the continuity guard must not treat them as corruption.


def _records_with_serials(values):
    return [{"sno": str(v), "_review_reasons": []} for v in values]


def test_serial_guard_flags_backwards_or_repeated_serials():
    import ocr_text_pdf_api as service

    page = _records_with_serials([10, 11, 9, 12])
    service._flag_broken_serial_run(page, [])
    assert page[0]["_review_reasons"]
    assert "serial_run_broken" in page[0]["_review_reasons"][0]


def test_serial_guard_allows_gaps_in_the_addendum():
    """The addendum skips cards and restarts per section; that is not a defect."""
    import ocr_text_pdf_api as service

    for serials in ([57, 58, 59, 64, 65, 76], [1, 2, 3, 4]):
        page = _records_with_serials(serials)
        service._flag_broken_serial_run(page, [])
        assert not page[0]["_review_reasons"], serials


def test_serial_guard_is_silent_when_serials_are_unreadable():
    import ocr_text_pdf_api as service

    page = [{"sno": "", "_review_reasons": []} for _ in range(4)]
    service._flag_broken_serial_run(page, [])
    assert not any(r["_review_reasons"] for r in page)


def test_addendum_markers_are_read():
    """E/S/R are printed at the card's right edge, outside the serial window."""
    import ocr_text_pdf_api as service

    ward_pdf = SAMPLE_DIR / "JAIPUR NAGAR NIGAM-Ward No-028-Part No-001.pdf"
    if not ward_pdf.exists():
        pytest.skip("sample ward roll not present")

    import fitz

    found = set()
    with fitz.open(ward_pdf) as doc:
        for page in doc:
            for (ox, oy, _cid) in service._card_anchors(page):
                marker = service._ascii_values_from_text_layer(
                    page, ox, oy).get("deletion_marker")
                if marker:
                    found.add(marker)
    assert found, "expected E/S/R markers in the addendum"
    assert found <= set(service._SERIAL_MARKERS), found


def test_marker_window_does_not_collide_with_the_serial_window():
    """The marker sits past the serial window, so it cannot be read as one."""
    import ocr_text_pdf_api as service

    assert service._SERIAL_MARKER_WINDOW[0] > service.SERIAL_WINDOW[1]
    assert service._SERIAL_MARKER_WINDOW[0] < service.CARD_WIDTH_PT
