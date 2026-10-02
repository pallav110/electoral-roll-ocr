"""Guards that decide whether a neural transliteration may be used at all.

IndicTrans2 is a translation model, so it fails in ways a rule cannot: it
answers a different question (चन्द्र -> "the moon"), it degenerates into a
repetition loop, and it detaches combining marks from their base letters.
None of those raise an exception. They arrive as a confident English string
that is wrong.

Every guard here is a *rejection*, never a repair. A guard that "fixes" output
is a second transliteration layer with none of the auditability.

This lives in its own module because both evaluation scripts need it, and two
copies of a drift list will eventually disagree -- at which point the
disagreement shows up as a wrong name in a database column.
"""
from __future__ import annotations

import re
import unicodedata

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
DIGIT_RE = re.compile(r"\d")

# Repetition of the same 2-30 char unit three or more times. Real names do not
# repeat; "Ram Ram" is not a name. The window is wide because the model
# repeats at the word ("the limit of the limit of") as often as the character.
REPEAT_RE = re.compile(r"(.{2,30}?)\1{2,}")

# A run of the same single character -- most often the ideographic full stop
# 。, or a bare '.' from a broken tokenizer. A single-character run needs its
# own pattern because the 2+ char window above cannot match it: "bhikkum...."
# repeats nothing, it just trails off into punctuation. Five is generous for a
# real name; the longest legitimate run in the corpus is far shorter.
SINGLE_RUN_RE = re.compile(r"(.)\1{4,}")

# Translations that are correct English and the wrong answer for a name or a
# label. Built from the measured disagreements rather than written out in
# advance: a list holding only the drift you already know about will miss the
# next one, which is the whole failure mode.
SEMANTIC_DRIFT = {
    "the sky", "joy", "imagination", "relax", "peace", "wealth", "the moon",
    "sun", "lord", "goddess", "flower", "the earth", "student", "teacher",
    "king", "queen", "brave", "new", "the best", "red", "lotus", "prince",
    "hero", "blessings", "shoots", "alphabetic", "secret", "jokey", "god",
    "rise", "knowledge", "archer", "smell", "saints", "descendants",
    "the world", "the poor", "the feast", "the light", "value", "ask for",
    "donation", "the men's", "husband", "father", "mother", "woman", "man",
    "sunny", "satisfaction", "diamond", "hajj", "line", "huge", "faith",
    "green", "truth", "victory", "dream", "dreaming", "wish", "light",
    "death", "life", "money", "gold", "water", "fire", "wind", "stone",
}


def normalise(value: str) -> str:
    """Compare on letters and spaces only.

    The model writes "Rajesh." and "V.K. Tomar" with stray punctuation. Left
    in, those look like disagreements and bury the real ones under noise.
    """
    return "".join(
        ch for ch in (value or "").strip().lower() if ch.isalnum() or ch.isspace()
    ).strip()


def has_orphan_marks(english: str) -> bool:
    """Combining marks left in the output mean a diacritic was detached from
    its base letter: कृष्ण -> "Kr ̣ s ̣ n ̣ a". Invisible in a diff,
    catastrophic in a name column."""
    return any(
        unicodedata.category(ch) in ("Mn", "Mc")
        for ch in unicodedata.normalize("NFD", english or "")
    )


def is_drift(english: str) -> bool:
    low = normalise(english)
    return bool(low) and (low in SEMANTIC_DRIFT or low.startswith("the "))


def guard_reasons(hindi: str, model: str) -> list[str]:
    """Every reason to REJECT the model's answer. Empty means usable.

    `hindi` is needed for the length and digit checks; passing "" disables
    those two rather than misfiring on an empty source.
    """
    out = model or ""
    reasons = []

    if not out.strip():
        return ["empty"]
    if DEVANAGARI_RE.search(out):
        reasons.append("devanagari residue")
    if has_orphan_marks(out):
        reasons.append("orphan combining marks")
    if REPEAT_RE.search(out) or SINGLE_RUN_RE.search(out):
        reasons.append("degenerate repetition")
    if is_drift(out):
        reasons.append("meaning drift")
    if hindi and len(out) > 3 * max(len(hindi), 4):
        reasons.append("runaway length")

    # Digits are load-bearing: a wrong digit points at the wrong house.
    if set(DIGIT_RE.findall(hindi or "")) - set(DIGIT_RE.findall(out)):
        reasons.append("digits lost")

    return reasons


def is_usable(hindi: str, model: str) -> bool:
    return not guard_reasons(hindi, model)