"""A Paddle age read must come from a crop that actually holds printed text.

The age is the only field whose arbitration is a bare range check -- `18 <= n <=
120` and nothing else -- so it is the only field where a plausible-looking number
can be fabricated from pixels that contain no number at all.

That is exactly what happened on the S28-40 Lansdown roll.  AGE_REGION is
calibrated on the S24-53 template, where the printed `आयु :` row falls inside it.
On Lansdown the row sits higher, the crop lands in blank white below it, and:

    _extract_age_from_clean_card -> ''    (correctly refused)
    after the Paddle pass        -> '88'  (two digits read out of noise)

The card prints `आयु : 41`.  These tests pin the guard so the crop's ink is
checked before its digits can overwrite anything.
"""
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

import ocr_pdf_api as A  # noqa: E402


def _crop_with_text(height: int, width: int, fraction: float) -> np.ndarray:
    """A crop with `fraction` of its pixels dark -- a printed row or blank."""
    crop = np.full((height, width, 3), 255, dtype=np.uint8)
    inked = int(height * width * fraction)
    if inked:
        crop.reshape(-1, 3)[:inked] = 20
    return crop


def test_blank_crop_has_no_ink():
    """The failure being guarded: an all-white crop."""
    assert A._crop_has_ink(_crop_with_text(120, 400, 0.0)) is False


def test_printed_row_has_ink():
    """A crop holding a printed row must pass the guard."""
    # ~2% ink is what the age crop measures on the template it was calibrated
    # on, so this is the case the guard must NOT block.
    assert A._crop_has_ink(_crop_with_text(120, 400, 0.02)) is True


def test_ink_fraction_is_relative_to_crop_area():
    """The same ink per area must pass at any crop size.

    An absolute pixel threshold would mean something different on a 2806pt page
    and an 842pt one, which is the whole class of bug this guard exists to stop.
    """
    for height, width in ((120, 400), (40, 130), (300, 900)):
        assert A._crop_has_ink(_crop_with_text(height, width, 0.02)) is True, (height, width)


def test_none_and_empty_crops_are_rejected():
    assert A._crop_has_ink(None) is False
    assert A._crop_has_ink(np.zeros((0, 0, 3), dtype=np.uint8)) is False


def test_grayscale_crop_is_accepted():
    """The guard must handle a 2-D crop as well as a 3-D one."""
    gray = np.full((120, 400), 255, dtype=np.uint8)
    gray[:6, 20:380] = 20
    assert A._crop_has_gray_ink(gray) if hasattr(A, "_crop_has_gray_ink") \
        else A._crop_has_ink(gray) is True


def test_a_bare_border_does_not_qualify():
    """A card rule is ink, but it is not an age.

    A 1px frame around a 400x120 crop is ~0.7% of its area.  Counting the frame
    as evidence would let a crop containing only a box pass, so the guard
    discounts ink that sits on the crop's own edge -- a rule is on the boundary
    by definition, and a printed digit never is.
    """
    box = np.full((120, 400, 3), 255, dtype=np.uint8)
    box[0, :, :] = 0
    box[-1, :, :] = 0
    box[:, 0, :] = 0
    box[:, -1, :] = 0
    assert A._crop_has_ink(box) is False


def test_ink_touching_one_edge_still_counts():
    """A rule on one side must not disqualify a real printed row.

    The age crop legitimately abuts the card's left border, so only ink that is
    edge ink on ALL four sides is treated as a frame.
    """
    row = np.full((120, 400, 3), 255, dtype=np.uint8)
    row[40:44, 20:200] = 20          # a printed digit row, well inside
    row[:, 0, :] = 0                # one card rule, on the left edge
    assert A._crop_has_ink(row) is True


def test_paddle_age_overwrite_is_gated_on_ink():
    """The overwrite must be inside the ink check, and the check must come first.

    Pinned on the source so the guard cannot be lifted off the assignment while
    the function still reads as correct.
    """
    import inspect

    source = inspect.getsource(A)
    start = source.index('elif field == "age" and field_texts')
    age_branch = source[start:start + 2000]
    # The label must be checked on the WIDE crop.  The crop Paddle actually reads
    # has been narrowed by `_age_paddle_crop`, which crops the label away by
    # construction, so checking that one would fail on every card.
    assert "_crop_shows_age_row(age_img_wide)" in age_branch, \
        "the Paddle age overwrite is no longer gated on the age label"
    assign = 'rec["age"] ='
    assert assign in age_branch, "the age assignment moved out of this branch"
    assert (age_branch.index("_crop_shows_age_row(age_img_wide)")
            < age_branch.index(assign)), \
        "the label check must run before the age is overwritten"


def test_age_range_alone_is_not_sufficient():
    """88 is in range and is still wrong.  Document the trap being avoided."""
    assert A.AGE_MIN <= 88 <= A.AGE_MAX