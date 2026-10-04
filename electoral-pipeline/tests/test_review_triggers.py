"""The review triggers must flag what needs checking and nothing else.

Two failure modes matter, and they are opposites:

  * Under-flagging -- a record with a missing age or a junk house value ships
    unmarked, and the flag becomes a claim of confidence it has not earned.
  * Over-flagging -- healthy records get flagged, people learn the flag means
    nothing, and the one signal this system has for "check me" stops working.

The "nothing else" half is the harder one to test and the more valuable, so
every rule has both a positive case (must fire) and a healthy case (must not).

`_review_record` is deliberately pure: it reads a record and adds flags. These
tests assert it never CHANGES a value, because "flagged" and "corrected" are
different claims and a trigger that rewrites the field would make them the same.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_pdf_api import (  # noqa: E402
    AGE_MAX,
    AGE_MIN,
    _classify_devanagari_house,
    _devanagari_consonants,
    _mark_review,
    _review_record,
)


def _record(**kw):
    """A healthy record, then overridden by the test."""
    base = {
        "age": "45",
        "house_no": "142",
        "id_card_no": "ABC1234567",
        "voter_first_name": "राम",
    }
    base.update(kw)
    return base


def _reasons(rec):
    return rec.get("_review_reasons", [])


# ── age ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("age", ["18", "21", "45", "60", str(AGE_MAX)])
def test_valid_age_is_not_flagged(age):
    rec = _record(age=age)
    _review_record(rec)
    assert not rec.get("_needs_review"), _reasons(rec)


@pytest.mark.parametrize("age", [None, "", "   "])
def test_missing_age_is_flagged(age):
    rec = _record(age=age)
    _review_record(rec)
    assert rec.get("_needs_review")
    assert "age_absent" in _reasons(rec)


@pytest.mark.parametrize("age", ["17", "0", "1", "121", "200"])
def test_age_outside_the_voter_range_is_flagged(age):
    rec = _record(age=age)
    _review_record(rec)
    assert rec.get("_needs_review")
    assert any(r.startswith("age_out_of_range") for r in _reasons(rec)), _reasons(rec)


def test_boundaries_are_inside_the_range():
    """AGE_MIN and AGE_MAX are valid ages, not out-of-range ones."""
    for age in (str(AGE_MIN), str(AGE_MAX)):
        rec = _record(age=age)
        _review_record(rec)
        assert not any(
            r.startswith("age_out_of_range") for r in _reasons(rec)
        ), (age, _reasons(rec))


@pytest.mark.parametrize("age", ["4 5", "forty", "45岁", "4a"])
def test_non_integer_age_is_flagged_as_such_not_as_range(age):
    rec = _record(age=age)
    _review_record(rec)
    assert "age_not_integer" in _reasons(rec), _reasons(rec)


def test_integer_age_carries_no_not_integer_flag():
    rec = _record(age=45)          # an int, not a str
    _review_record(rec)
    assert "age_not_integer" not in _reasons(rec), _reasons(rec)


# ── house ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("house", ["142", "E-8/496", "8/496", "47-E-7", "279/79"])
def test_valid_house_is_not_flagged(house):
    rec = _record(house_no=house)
    _review_record(rec)
    assert not rec.get("_needs_review"), _reasons(rec)


@pytest.mark.parametrize("house", [None, "", "  "])
def test_missing_house_is_flagged(house):
    rec = _record(house_no=house)
    _review_record(rec)
    assert "house_absent" in _reasons(rec), _reasons(rec)


def test_devanagari_junk_in_a_house_value_is_flagged():
    """The p11c16 shape: 'व्दव 1141' -- junk plus a number.

    Confirmed against the pixels by the user: the true value is 141 and
    'व्दव' is OCR noise. This is the single house defect known to be real,
    so the rule that misses it is worse than no rule.
    """
    rec = _record(house_no="व्दव 1141")
    _review_record(rec)
    assert any(
        r.startswith("house_devanagari_unknown") for r in _reasons(rec)
    ), _reasons(rec)


@pytest.mark.parametrize("house", ["तर 42", "छ्लो-9", "क्रज"])
def test_other_unknown_devanagari_is_flagged(house):
    rec = _record(house_no=house)
    _review_record(rec)
    assert any(
        r.startswith("house_devanagari_unknown") for r in _reasons(rec)
    ), _reasons(rec)


@pytest.mark.parametrize(
    "house",
    [
        # Label words only -- the roll prints these and the user said the
        # words are noise to be dropped, not errors.
        "हाऊस नं- 453",
        "मकान संख्या €- 854",
        "एचएनओ 146",
        "प्लॉट नं 279 ख नं 79",
        "पी. नं-बी 190, ख नं-701",
        "इ-857 गली न॑.8",
        # Plot letters -- the user asked for these to be PRESERVED.
        "इ-8/496",        # the E-8/496 case the user confirmed is written in English
        "7 बी",
        "सी-22",
        "47-ई-7",
        "8इ-526",
        "174/इ-10",
        "15 ए",
        "जा 107",
        "शी",
        "ग 821",
    ],
)
def test_correct_devanagari_house_is_NOT_flagged(house):
    """Over-flagging is the failure that costs the most.

    Every value here is either a label the user said to ignore or a plot
    letter the user said to preserve. Flagging them would put 89 of 531
    records into the review queue and the flag would stop meaning anything.
    """
    rec = _record(house_no=house)
    _review_record(rec)
    assert not any(
        r.startswith("house_devanagari") for r in _reasons(rec)
    ), (house, _reasons(rec))


# ── the classifier itself ──────────────────────────────────────────────────
#
# These pin the two bugs that made the rule silently dead. Both returned a
# plausible verdict, so neither would have shown up as a crash.

@pytest.mark.parametrize(
    "run,expected",
    [
        ("बी", 1),      # b + ii-matra: one consonant
        ("इ", 1),
        ("ई", 1),
        ("शी", 1),
        ("ये", 1),
        ("ग", 1),
        ("व्दव", 3),    # v + virama + d + v: THREE, the virama is not a consonant
        ("तर", 2),
        ("छ्लो", 2),    # the virama again
    ],
)
def test_consonant_count_ignores_marks(run, expected):
    assert _devanagari_consonants(run) == expected, run


@pytest.mark.parametrize(
    "house,verdict",
    [
        ("व्दव 1141", "unknown"),   # the confirmed defect -- must not be 'plot'
        ("तर 42", "unknown"),
        ("छ्लो-9", "unknown"),
        ("इ-8/496", "plot"),
        ("7 बी", "plot"),
        ("सी-22", "plot"),
        ("47-ई-7", "plot"),
        ("हाऊस नं- 453", "clean"),
        ("एचएनओ 146", "clean"),
        ("प्लॉट नं 279 ख नं 79", "clean"),
        ("142", "clean"),
    ],
)
def test_classifier_verdicts(house, verdict):
    assert _classify_devanagari_house(house)[0] == verdict, house


def test_classifier_finds_multi_consonant_runs_not_single_characters():
    """Guards the `findall` bug.

    `re.findall(r'[ऀ-ॿ]')` yields single characters, so every run looked like
    one consonant and every value classified as 'plot' -- a rule that flags
    nothing while appearing to work.
    """
    _, runs = _classify_devanagari_house("व्दव 1141")
    assert runs == ["व्दव"], runs


def test_punctuation_between_two_plot_letters_does_not_glue_them():
    """Guards the glue bug, on the value the user personally explained.

    'पी. नं-बी 190, ख नं-701' is plot 190 / khasra 701 -- one address, two
    numbers. Deleting the '.', '-' and ',' instead of replacing them with a
    space leaves 'पीबी', which is two consonants and so classifies as junk.

    The verdict is 'plot', not 'clean': after the labels 'नं' and 'ख' are
    stripped, the leftovers 'पी' and 'बी' ARE plot letters. Either verdict is
    unflagged -- only 'unknown' flags -- so the distinction that matters is
    that it is not 'unknown'.
    """
    verdict, runs = _classify_devanagari_house("पी. नं-बी 190, ख नं-701")
    assert verdict in ("clean", "plot"), (verdict, runs)
    rec = _record(house_no="पी. नं-बी 190, ख नं-701")
    _review_record(rec)
    assert not any(
        r.startswith("house_devanagari") for r in _reasons(rec)
    ), _reasons(rec)


# ── the reconstructed leading 1 ─────────────────────────────────────────────

def test_prepended_leading_1_is_flagged():
    rec = _record(house_no="15 ए", _house_leading_1_prepended=True)
    _review_record(rec)
    assert any(r.startswith("house_leading_1_prepended") for r in _reasons(rec))


def test_house_reading_the_same_digits_is_not_flagged():
    """'15 ए' arrived by reading, not by reconstruction -- different claim."""
    rec = _record(house_no="15 ए")
    _review_record(rec)
    assert not any(
        r.startswith("house_leading_1") for r in _reasons(rec)
    ), _reasons(rec)


# ── the contract that makes the flag meaningful ────────────────────────────

def test_trigger_never_changes_a_value():
    """Flagging must not rewrite. 'flagged' != 'corrected'."""
    before = {
        "age": "4 5", "house_no": "व्दव 1141",
        "id_card_no": "ABC1234567", "voter_first_name": "राम",
    }
    # 'व्दव 1141' and '4 5' are both wrong. The reviewer must still leave them
    # exactly as found -- it flags, it does not repair.
    rec = _record(**before)
    for key, value in before.items():
        rec[key] = value
    _review_record(rec)
    assert rec["_needs_review"]
    for key, value in before.items():
        assert rec[key] == value, (key, rec[key], value)


def test_mark_review_is_idempotent():
    rec = _record()
    _mark_review(rec, "age_absent")
    _mark_review(rec, "age_absent")
    assert _reasons(rec).count("age_absent") == 1


def test_mark_review_preserves_reasons_from_the_epic_vote():
    """The EPIC split already set a flag and a reason; neither may be lost."""
    rec = _record()
    rec["_needs_review"] = True
    rec["_review_reasons"] = ["epic_readers_disagree: Xx4 Yx4"]
    _mark_review(rec, "age_absent")
    assert rec["_needs_review"]
    assert _reasons(rec) == ["epic_readers_disagree: Xx4 Yx4", "age_absent"]


def test_several_problems_on_one_record_all_get_reported():
    rec = _record(age="17", house_no="व्दव 1141")
    _review_record(rec)
    reasons = _reasons(rec)
    assert any(r.startswith("age_out_of_range") for r in reasons), reasons
    assert any(r.startswith("house_devanagari") for r in reasons), reasons
    assert len(reasons) == 2, reasons


def test_a_fully_healthy_record_raises_nothing():
    rec = _record()
    _review_record(rec)
    assert not rec.get("_needs_review"), _reasons(rec)
    assert "_review_reasons" not in rec