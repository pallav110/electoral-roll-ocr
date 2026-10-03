"""The voter's name and the relation's name must land in different fields.

Tesseract frequently returns the voter's name row and the relation row as a
single line:

    'नाम : सचिन मेता पिता का नाम: राजकुमार'

The parser used to decline any line carrying a relation label and then split
at the FIRST colon, so the voter's own name was written into voter_father_name
and voter_first_name was left empty -- 479 of 531 records on the live roll,
with 467 of them carrying the literal 'का नाम' in the relation field.

Every case below is a verbatim line captured from the roll or read out of its
own text layer (input/copied_pdf.txt), not an invented string.

Before the fix: 'merged_husband', 'relation_word_in_name' and 'merged_mother'
returned {"empty": True} -- those cards lost entirely, not just mis-split.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ocr_pdf_api import parse_voter_box_from_ocr_lines  # noqa: E402


def _parts(record: dict) -> str:
    """The voter's own name, rejoined from its three component fields."""
    return " ".join(filter(None, (
        record.get("voter_first_name", ""),
        record.get("voter_middle_name", ""),
        record.get("voter_sur_name", ""),
    )))


@pytest.mark.parametrize("lines,expected_name,field,expected_relative", [
    # The two fields arrive glued into one line; both must survive.
    (['नाम : सचिन मेता पिता का नाम: राजकुमार'],
     "सचिन मेता", "voter_father_name", "राजकुमार"),
    (['नाम: ममता देवी पति का नाम: सुनील कुमार'],
     "ममता देवी", "voter_husband_name", "सुनील कुमार"),
    # Three-token relative name: the middle token must not be promoted.
    (['नाम : कल्पना पति का नाम: मनोज कुमार शर्मा'],
     "कल्पना", "voter_husband_name", "मनोज कुमार शर्मा"),
    # OCR misread of the husband label, read from a real card.
    (['नाम: धनपति देवी प्रति का नाम: काशीराम'],
     "धनपति देवी", "voter_husband_name", "काशीराम"),
    (['नाम: सुनीता माता का नाम: पूनम सक्सेना'],
     "सुनीता", "voter_mother_name", "पूनम सक्सेना"),
    # The photo watermark is stamped over the photo box and bleeds into the
    # text rows beside it. It must never become part of a name.
    (['नाम : सचिन मेता फोटो उपलब्ध है पिता का नाम: राजकुमार'],
     "सचिन मेता", "voter_father_name", "राजकुमार"),
])
def test_merged_line_separates_voter_from_relative(lines, expected_name, field, expected_relative):
    record = parse_voter_box_from_ocr_lines(list(lines))
    assert not record.get("empty"), record
    assert _parts(record) == expected_name
    assert record.get(field) == expected_relative


@pytest.mark.parametrize("line,field,expected_relative", [
    # Pure relation rows must parse exactly as they always have.
    ('पिता का नाम: राजकुमार', "voter_father_name", "राजकुमार"),
    ('पति का नामः मनोज कुमार शर्मा', "voter_husband_name", "मनोज कुमार शर्मा"),
    ('माता का नाम: महेश पांचाल', "voter_mother_name", "महेश पांचाल"),
    # Glued label forms the roll actually prints. The existing patterns
    # already match all of these -- \s*(?:का)?\s* absorbs the missing space --
    # so these are guards against a future "tighten the regex" regression.
    ('पिता कानाम: राजकुमार', "voter_father_name", "राजकुमार"),
    ('पतिका नाम: प्रेम चंद', "voter_husband_name", "प्रेम चंद"),
    ('पिताका नाम: धर्मेंद्र', "voter_father_name", "धर्मेंद्र"),
    ('अन्यः कुवरपाल', "voter_other_name", "कुवरपाल"),
])
def test_pure_relation_lines_are_unchanged(line, field, expected_relative):
    # These lines carry no age or gender, so the function's return gate
    # rejects a record built from them alone; that gate is pre-existing and
    # unchanged. Pair each with an age so the record is returned.
    record = parse_voter_box_from_ocr_lines([line, "आयु : 34 लिंग : पुरुष"])
    assert not record.get("empty"), record
    assert record.get(field) == expected_relative
    assert record["voter_first_name"] == "", "a relation row is not a voter name"


def test_photo_watermark_never_becomes_a_name():
    """A watermark-prefixed relation row must yield no name at all."""
    record = parse_voter_box_from_ocr_lines(
        ['फोटो उपलब्ध है पिता का नाम: आराम सिंह', "आयु : 45 लिंग : पुरुष"]
    )
    assert not record.get("empty"), record
    assert _parts(record) == ""
    assert record["voter_father_name"] == "आराम सिंह"


def test_relation_word_inside_a_real_name_is_not_a_relation_label():
    """'धनपति देवी' is a voter, not a husband label.

    The word-boundary guard is what makes this safe; without it the 'पति'
    inside the surname is read as the start of the relation row and the whole
    card is lost.
    """
    record = parse_voter_box_from_ocr_lines(
        ['नाम: धनपति देवी पति का नाम: काशीराम']
    )
    assert not record.get("empty"), record
    assert _parts(record) == "धनपति देवी"
    assert record["voter_husband_name"] == "काशीराम"


def test_duplicate_relation_labels_do_not_become_a_name():
    """Two relation labels on one line: no split, and no label in the name.

    Splitting here would write 'पिता कानाम इंद्र पाल' into voter_first_name --
    a label plus a colon, which is worse than leaving the name blank.
    """
    record = parse_voter_box_from_ocr_lines(
        ['पिता कानाम इंद्र पाल पिता का नाम: महेश पति का नामः प्रमोद', "आयु : 51"]
    )
    assert ":" not in _parts(record)
    assert "का नाम" not in _parts(record)


def test_house_number_sharing_a_relation_row_is_recovered():
    """A house number printed on the relation row used to be dropped.

    The relation branch claims the line and continue()s, so 13 rows of this
    roll lost their house number entirely.
    """
    record = parse_voter_box_from_ocr_lines(
        ['पिता का नामः राजकुमार फोटो उपलब्ध है मकान संख्या: 00 फोटो उपलब्ध है']
    )
    assert not record.get("empty"), record
    assert record["voter_father_name"] == "राजकुमार"
    assert record["house_no"] == "00"


def test_age_and_gender_still_parse_together():
    record = parse_voter_box_from_ocr_lines(['आयु : 58 लिंग : पुरुष'])
    assert record["age"] == "58"
    assert record["gender"] == "पुरुष"


def test_unlabelled_name_line_still_produces_no_name():
    """A name with no label cannot be attributed -- unchanged behaviour.

    The function has no positional fallback, so an unlabelled line yields
    nothing. Asserted so a future 'just guess the first line is the name'
    change is caught: on a card with no name label, guessing would invent
    names out of the address row.
    """
    record = parse_voter_box_from_ocr_lines(
        ['सचिन मेता', 'पिता का नाम: राजकुमार', "आयु : 34"]
    )
    assert record["voter_first_name"] == ""
    assert record["voter_father_name"] == "राजकुमार"
