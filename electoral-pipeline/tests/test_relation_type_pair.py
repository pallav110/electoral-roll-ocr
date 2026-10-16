"""`relation_name` and `relationship_type` are one fact in two columns.

The two are read from two different fields in the OCR payload: the English
side from `common.relationship_type`, the Hindi side from
`raw_record.relation_name`. Nothing kept them in step, so a deployment that
passed one and dropped the other wrote rows with the English relation
populated and the Hindi relation NULL -- 13,113 rows across 17 PDFs on
2026-10-05, from HIN-565 to HIN-582.

On screen that reads as a half-empty record rather than as missing data: the
Relative type column is blank while Relationship (English) says "father".
The split is only visible by joining the two columns.

The relation labels are a closed, 1:1 set, so the English key determines the
Hindi word exactly. `_relation_type_hi` is that reverse lookup; these tests pin
it against guessing, which is the one thing it must not do.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.normalize import _RELATION_TYPE_HI, _relation_type_en, _relation_type_hi

# The four labels actually printed on the roll. extractor._RELATION_KEY is the
# forward map; this is its exact inverse.
PRINTED_LABELS = {
    "father": "पिता",
    "husband": "पति",
    "mother": "माता",
    "other": "अन्य",
}


@pytest.mark.parametrize("key,hindi", sorted(PRINTED_LABELS.items()))
def test_english_key_recovers_the_printed_hindi_label(key, hindi):
    """The repair that recovered the 13k stale rows."""
    assert _relation_type_hi(key) == hindi


@pytest.mark.parametrize("key", sorted(PRINTED_LABELS))
def test_round_trips_through_both_directions(key):
    """Hindi → English → Hindi must be the identity.

    The two columns are filled by independent code paths, so this is what
    guarantees they can never disagree about a label they both hold.
    """
    hindi = PRINTED_LABELS[key]
    assert _relation_type_hi(_relation_type_en(hindi)) == hindi


@pytest.mark.parametrize("value", ["son", "brother", "wife", "uncle", "फादर"])
def test_unrecognised_value_returns_none_and_never_guesses(value):
    """An unmapped key means the vocabulary grew.

    Falling back to `str(value)` here would put the raw English word in a
    Hindi column, and falling back to a default would put a *wrong* relation
    on a real voter. Both are worse than a blank, which is visible and
    reviewable.
    """
    assert _relation_type_hi(value) is None


def test_absent_value_returns_none():
    """A record with no relation word on either side stays genuinely empty."""
    assert _relation_type_hi(None) is None
    assert _relation_type_hi("") is None
    assert _relation_type_hi("   ") is None


@pytest.mark.parametrize("value", ["  FATHER ", "Father", " father"])
def test_key_matching_is_tolerant_of_case_and_padding(value):
    """`relationship_type` is written by several producers.

    `_relation_type_en` lowercases before matching, so the reverse lookup has
    to as well -- otherwise a differently-cased key repairs one column and
    not the other.
    """
    assert _relation_type_hi(value) == "पिता"


def test_reverse_map_covers_exactly_the_forward_vocabulary():
    """The two maps must not drift apart.

    extractor._RELATION_KEY is the forward map and is not imported here (it
    drags in the OCR stack), so this pins the expected key set instead. A key
    added to the extractor without a Hindi counterpart here is the exact bug
    this whole file exists to prevent.
    """
    assert set(_RELATION_TYPE_HI) == set(PRINTED_LABELS)


def test_no_key_maps_to_two_hindi_words():
    """The map must stay injective.

    A collision would make a single English key repair rows to two different
    relations, which is unrecoverable and undetectable by inspection.
    """
    values = list(_RELATION_TYPE_HI.values())
    assert len(values) == len(set(values))