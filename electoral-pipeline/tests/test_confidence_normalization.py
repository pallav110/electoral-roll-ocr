"""A bad confidence must cost one field, not the whole unit.

`map_record` did this:

    confidence = item.get("confidence")
    if confidence is not None and not 0 <= float(confidence) <= 1:
        errors.append("confidence outside 0–1")

Three defects, all found by running the real function over every input type
rather than reading it:

1. `float(confidence)` raised on anything non-numeric. `map_record` is called in
   a list comprehension over a whole unit's records, so ONE record whose
   confidence was "" or "high" raised ValueError and aborted all 30 -- the 29
   healthy records were discarded with it. An unusable confidence is a problem
   with one field of one record.

2. `bool` passed the range check, because `0 <= True <= 1` is True in Python. A
   record whose confidence was the JSON value `true` was stored with confidence
   True. `_int()` already refused bool for exactly this reason.

3. Out-of-range values were flagged in `errors` but still stored as-is, so
   `2.5` and `-0.1` landed in a `Numeric(5,4)` column.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _map(confidence, mark=None):
    """Run one record through the real map_record and return (confidence, errors)."""
    from app.normalize import map_record

    item = {
        "source": {"page_number": 3, "row_number": 1},
        "hindi": {},
        "english": {},
        "common": {},
        "raw_record": {},
        "confidence": confidence,
    }
    doc_id, unit_id = uuid.uuid4(), uuid.uuid4()
    record = map_record(item, doc_id, unit_id, unit_id)
    errors = [e for e in (record.validation_errors or []) if "confidence" in e]
    return record.confidence, errors


# --- 1. must not raise ------------------------------------------------------

@pytest.mark.parametrize(
    "junk", ["high", "", "  ", "n/a", "0.95x", [], {}, "nan", "inf", "-inf"]
)
def test_non_numeric_confidence_does_not_abort_the_record(junk):
    """The regression: these all raised ValueError and killed the unit."""
    confidence, errors = _map(junk)
    assert confidence is None, f"{junk!r} should not become a stored confidence"
    assert errors == ["confidence not a number"], (
        f"{junk!r} should be reported as unusable, got {errors}"
    )


def test_float_nan_is_rejected():
    """NaN is the one float that is not equal to itself, so != is the test.

    `0 <= nan <= 1` is False, so the range check alone would have flagged it but
    still stored it -- and NaN in a Numeric column poisons any aggregate it
    reaches.
    """
    confidence, errors = _map(float("nan"))
    assert confidence is None
    assert errors == ["confidence not a number"]


def test_infinite_confidence_is_rejected():
    """inf is a valid float, so float() accepts it. It is not a confidence."""
    for value in (float("inf"), float("-inf")):
        confidence, errors = _map(value)
        assert confidence is None, f"{value} should not be stored"
        assert errors == ["confidence not a number"]


# --- 2. bool must not pass the range check ----------------------------------

@pytest.mark.parametrize("flag", [True, False])
def test_bool_confidence_is_not_stored(flag):
    """`0 <= True <= 1` is True, so the old code stored the boolean itself."""
    confidence, errors = _map(flag)
    assert confidence is None, f"{flag!r} was stored as a confidence"
    assert errors == ["confidence not a number"]


def test_bool_is_still_rejected_by_int_helper():
    """_int() already did this; the confidence path had to match it."""
    from app.normalize import _int

    with pytest.raises(ValueError):
        _int(True, "confidence")


# --- 3. out of range is clamped, not stored as-is ---------------------------

@pytest.mark.parametrize(
    ("given", "stored"),
    [(2.5, 1.0), (-0.1, 0.0), (100, 1.0), (-100, 0.0), (1.0001, 1.0), (1, 1), (0, 0)],
)
def test_out_of_range_is_clamped_and_flagged(given, stored):
    """A record is kept; the value is bounded; the problem stays visible."""
    confidence, errors = _map(given)
    assert confidence == stored, f"{given!r} should clamp to {stored}"
    if given in (1, 0):
        assert errors == [], "boundary values are in range and must not be flagged"
    else:
        assert errors == ["confidence outside 0–1"], f"{given!r} should be flagged"


# --- and the values that were always fine -----------------------------------

@pytest.mark.parametrize("value", [0.0, 0.5, 0.95, 1.0, "0.95", 1])
def test_valid_confidence_passes_through_untouched(value):
    confidence, errors = _map(value)
    assert confidence == float(value)
    assert errors == []


def test_absent_confidence_is_none_and_not_an_error():
    """None means "not reported", which is different from "reported badly"."""
    confidence, errors = _map(None)
    assert confidence is None
    assert errors == []


# --- the unit-level consequence ---------------------------------------------

def test_one_bad_record_does_not_destroy_its_neighbours():
    """The actual blast radius, stated as a test.

    Before the fix, a list comprehension over a unit's records meant record 15
    raising took records 16-30 with it. This asserts the batch survives a bad
    element, which is the property the unit depends on.
    """
    from app.normalize import map_record

    doc_id, unit_id = uuid.uuid4(), uuid.uuid4()
    confidences = [0.9, "high", 0.8, True, None, 2.5, 0.7]

    def build(conf):
        return {
            "source": {"page_number": 3, "row_number": 1},
            "hindi": {},
            "english": {},
            "common": {},
            "raw_record": {},
            "confidence": conf,
        }

    records = [
        map_record(build(c), doc_id, unit_id, unit_id) for c in confidences
    ]

    assert len(records) == len(confidences), "every record should survive"
    assert [r.confidence for r in records] == [0.9, None, 0.8, None, None, 1.0, 0.7]