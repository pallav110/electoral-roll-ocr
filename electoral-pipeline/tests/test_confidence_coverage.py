"""A field an engine failed to read must not raise the confidence score.

Every voter record reports a single `confidence` number, stored as
`Numeric(5,4)` and shown in the UI as "Confidence". Before this fix that
number was computed by averaging the *non-zero* PaddleOCR field scores:

    valid_confs = [c for c in paddle_conf.values() if c > 0]
    paddle_avg = sum(valid_confs) / len(valid_confs)

which inverted the meaning of the score in two separate ways.

1. **A miss was deleted from the average.** PaddleOCR records a field it
   could not read as 0.0 (`field_confidences[field] = ... if field_confs
   else 0.0`). Filtering those zeros out meant a card PaddleOCR read
   nothing on scored *better* than one it read correctly, because the
   failures simply stopped contributing.

2. **A total failure scored as a total success.** When every field scored
   0.0 the filtered list was empty, `paddle_avg` became None, and the
   function fell through to `return tess_norm` at full weight. So
   "PaddleOCR read nothing" was numerically identical to "PaddleOCR never
   ran" and to "PaddleOCR agreed with Tesseract".

Measured on the 531-record frozen baseline: every record scored between
0.7374 and 0.9660, mean 0.9138. A score that cannot fall below 0.74 on
a roll containing blank tail pages, struck-through deletions and cut
names cannot be measuring what it claims to.

The fix separates two questions that one number was answering badly:

    confidence -- of the text that WAS read, how much do we trust it?
    coverage    -- what fraction of the fields we attempted came back?

and reports them as separate outputs. Confidence alone cannot distinguish
"read every field correctly" from "read one field correctly", because
both are a mean over the same single correct value.

These tests call the real function. Reimplementing the arithmetic here
would let the regression pass, which is the exact failure mode the
tesseract-line-grouping test was written to avoid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ocr_pdf_api import _compute_confidence  # noqa: E402


def test_a_missed_field_dragged_the_average_down_not_up():
    """The core regression: adding a zero must lower the score.

    The old code filtered zeros out, so this pair scored identically.
    """
    perfect = {"serial": 0.9, "house": 0.9, "age": 0.9}
    with_a_miss = {"serial": 0.9, "house": 0.9, "age": 0.0}

    good_conf, good_cov = _compute_confidence(90.0, perfect)
    miss_conf, miss_cov = _compute_confidence(90.0, with_a_miss)

    assert miss_conf < good_conf, (
        "a field PaddleOCR failed to read did not lower the confidence. "
        "The zeros are being filtered out before the mean, so a card read "
        "correctly and a card with a field missing score the same."
    )
    assert miss_cov < good_cov


def test_total_paddle_failure_is_not_reported_as_full_confidence():
    """All-zero Paddle used to return bare tess_norm, at full weight.

    This is the worst case and it produced the *highest* score of any
    input, because an empty filtered list looked exactly like "no Paddle
    evidence, use Tesseract alone".
    """
    confidence, coverage = _compute_confidence(95.0, {"serial": 0.0, "house": 0.0, "age": 0.0})

    tess_only = _compute_confidence(95.0, None)

    assert confidence is not None and tess_only[0] is not None
    assert confidence < tess_only[0], (
        "PaddleOCR read nothing on every field but the score came back at "
        "full Tesseract weight; a total engine failure is being reported "
        "as a clean read"
    )
    assert coverage == 0.0, "no Paddle field returned a read"


def test_coverage_counts_fields_that_returned_a_read():
    """Coverage is the hit rate over attempted fields, not a fixed number."""
    two_of_three = _compute_confidence(None, {"serial": 0.9, "house": 0.0, "age": 0.9})
    all_three = _compute_confidence(None, {"serial": 0.9, "house": 0.9, "age": 0.9})

    assert two_of_three[1] == pytest.approx(2 / 3)
    assert all_three[1] == pytest.approx(1.0)


def test_high_confidence_over_a_thin_read_is_distinguishable():
    """The reason coverage is a second number.

    One perfect field and three perfect fields have the same mean, so
    confidence alone cannot tell them apart. Coverage can.
    """
    one_field = _compute_confidence(None, {"serial": 1.0})
    three_fields = _compute_confidence(None, {"serial": 1.0, "house": 1.0, "age": 1.0})

    assert one_field[0] == three_fields[0] == pytest.approx(1.0)
    assert one_field[1] == pytest.approx(1.0)
    assert three_fields[1] == pytest.approx(1.0)

    # The real distinction shows up once a read is missed: one field missed
    # out of three is a very different extraction from one missed out of one.
    thin = _compute_confidence(None, {"serial": 1.0, "house": 0.0, "age": 1.0})
    assert thin[0] < 1.0 and thin[1] == pytest.approx(2 / 3)


def test_no_evidence_at_all_is_none_not_zero():
    """Neither engine ran: there is nothing to be confident or unsure about.

    Returning 0.0 would put a fabricated number in the database and make
    'we never looked' look like 'we looked and found nothing'.
    """
    assert _compute_confidence(None, None) == (None, None)
    assert _compute_confidence(None, {}) == (None, None)


def test_unattempted_field_is_absent_and_a_missed_one_is_zero():
    """The key in the dict is the record of whether we tried.

    Tesseract supplies age often enough that the sequential pass skips the
    Paddle age crop (`if age_png_deferred and not tess_age_ok`). An absent
    'age' key means not attempted -- no evidence -- and must not be
    scored as a miss.
    """
    attempted = _compute_confidence(90.0, {"serial": 0.8, "house": 0.8})
    never_tried = _compute_confidence(90.0, {"serial": 0.8, "house": 0.8, "age": 0.0})

    assert attempted[0] > never_tried[0], (
        "an absent key and a 0.0 key must not be equivalent: one means the "
        "crop was skipped, the other means the crop was read and came back empty"
    )


def test_tesseract_confidence_is_normalised_and_clamped():
    """Tesseract reports 0-100; the response contract is 0-1.

    The clamp is new. Tesseract's mean can only exceed 100 if something
    upstream hands us a malformed number, and normalize._confidence() would
    then have to clamp it again on the way into the database.
    """
    assert _compute_confidence(50.0, None)[0] == pytest.approx(0.5)
    assert _compute_confidence(0.0, None)[0] == pytest.approx(0.0)
    assert _compute_confidence(150.0, None)[0] == pytest.approx(1.0)
    assert _compute_confidence(-5.0, None)[0] == pytest.approx(0.0)


def test_unusable_input_does_not_raise():
    """A bad number must cost one field, not the whole extraction.

    normalize.py already learned this lesson the hard way: `float(confidence)`
    raised on a non-numeric value inside a comprehension over a whole
    unit, discarding all 30 records. The same guard belongs here.

    An unusable value is scored as a miss (0.0), not skipped. Skipping it
    would shrink the denominator -- which is exactly the mean-over-
    successes this function exists to stop computing. A field PaddleOCR
    returned a garbage score for is a field we tried and did not get.
    """
    # Unusable Tesseract value: that engine contributes nothing at all,
    # which is different from reading it badly. Paddle alone carries the
    # result, so 0.9 stays 0.9 rather than being averaged against a 0.0.
    assert _compute_confidence("high", {"serial": 0.9})[0] == pytest.approx(0.9)
    assert _compute_confidence(float("nan"), {"serial": 0.9})[0] == pytest.approx(0.9)

    # Unusable Paddle value: the field was attempted and yielded nothing,
    # so it scores 0.0 and drags both numbers down.
    for junk in ("high", None, float("nan")):
        confidence, coverage = _compute_confidence(90.0, {"serial": junk})
        assert confidence == pytest.approx(0.45), (
            f"{junk!r} should count as a missed field (0.0), giving "
            "(0.9 + 0.0) / 2"
        )
        assert coverage == 0.0

    # inf is a valid float, so it survives float() and reaches the clamp --
    # where min(1.0, inf) would make it the *maximum* confidence. It counts
    # as a miss instead, same as the other unparseable values.
    assert _compute_confidence(90.0, {"serial": float("inf")})[0] == pytest.approx(0.45)


def test_both_engines_present_still_average():
    """The weighting itself is unchanged -- only the filtering was wrong.

    Guarding against an over-correction: the fix must not quietly reweight
    the two engines, which would move every score on the roll for a reason
    nobody asked for.
    """
    confidence, _ = _compute_confidence(80.0, {"serial": 1.0, "house": 1.0, "age": 1.0})
    assert confidence == pytest.approx((0.8 + 1.0) / 2)
