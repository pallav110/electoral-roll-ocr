"""House-number transliteration follows address convention, not word spelling.

A bare Devanagari vowel carrying a numeral is a flat *label*: इ-897 is flat
E-897, not the word "i897". The rule-based path rendered it I-897, which is
what IndicTrans2 was measured to get right and transliterate() got wrong.
"""
import pytest

from app.transliterate import house_en, transliterate


@pytest.mark.parametrize("hindi,expected", [
    # इ -> E: the flat-letter convention. This is the regression the whole
    # change exists for.
    ("इ-897", "E-897"),
    ("इ-85", "E-85"),
    ("इ-481", "E-481"),
    ("इ-889", "E-889"),
    ("इ-9/827", "E-9/827"),
    ("इ-10/602", "E-10/602"),
    # 8इ-526: vowel glued to the numeral with no separator.
    ("8इ-526", "8E-526"),
    # हाऊस नं इ5/245: letter attached to the number, no hyphen.
    ("हाऊस नं इ5/245", "Haus No. E5/245"),
    # ई is /i:/ -- "ee" in *see*. The English letter naming that sound is E.
    ("47-ई-7", "47-E-7"),
    ("इ-857 गली न॑.8", "E-857 Gali No..8"),
])
def test_flat_labels_read_as_letters(hindi, expected):
    assert house_en(hindi) == expected


@pytest.mark.parametrize("hindi,expected", [
    # Unaffected: these were already right and must stay right.
    ("बी-89", "B-89"),
    ("7 बी", "7 B"),
    ("15 ए", "15 E"),
    ("ए-73", "E-73"),
    ("व444", "Va444"),
    # A vowel inside a word, or in front of a non-numeral, must not become E.
    ("प्लॉट नं 279 ख नं 79", "Plot No. 279 Kha No. 79"),
    ("पी. नं-बी 190, ख नं-701", "P. No.-B 190, Kha No.-701"),
])
def test_ordinary_address_text_is_untouched(hindi, expected):
    assert house_en(hindi) == expected


def test_digits_are_never_altered():
    """A wrong digit points at the wrong house -- far worse than a wrong letter."""
    for hindi in ["इ-897", "इ-9/827", "47-ई-7", "प्लॉट नं 279 ख नं 79",
                  "पी. नं-बी 190, ख नं-701", "हाऊस नं इ5/245"]:
        out = house_en(hindi)
        import re
        assert set(re.findall(r"\d", hindi)) == set(re.findall(r"\d", out)), hindi


def test_names_still_use_word_spelling():
    """The E-for-ee rule is scoped to house numbers. सीता must stay Sita."""
    assert transliterate("सीता") == "Sita"


def test_house_en_handles_empty_input():
    assert house_en(None) is None
    assert house_en("") is None
    assert house_en("   ") is None
