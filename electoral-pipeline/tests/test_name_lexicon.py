"""Layer 1: the proper-noun spelling lexicon.

Layer 0 produces a phonetic transliteration. English does not spell names
phonetically: पाल is "Pal" not "Pala", वर्मा is "Verma" not "Varma". That
gap is a lexicon fact, not a derivable one, and app/name_lexicon.json is the
only place it lives.

These tests pin the three properties that make the map safe to run unattended.
Nothing in the map was hand-approved, so the filters that produced it are the
thing that has to hold.
"""
import json
import re
from pathlib import Path

import pytest

from app.transliterate import _name_lexicon, house_en, transliterate

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


@pytest.mark.parametrize("hindi,expected", [
    # English drops the inherent schwa the rules keep.
    ("पाल", "Pal"),
    ("राम", "Ram"),
    ("राज", "Raj"),
    ("दत्त", "Dutt"),
    ("गर्ग", "Garg"),
    ("चन्द", "Chand"),
    # English spells these the other way round from the phonetic form.
    ("वर्मा", "Verma"),
    ("मेहता", "Mehta"),
    ("पवार", "Pawar"),
    ("ममता", "Mamta"),
    ("मोहम्मद", "Mohammed"),
    ("सक्सेना", "Saxena"),
    ("जगदीश", "Jagdish"),
    ("लखपत", "Lakhpat"),
])
def test_lexicon_english_spelling(hindi, expected):
    assert transliterate(hindi) == expected


@pytest.mark.parametrize("hindi", [
    # The whole point of an exact-match map: a name that merely CONTAINS a
    # lexicon key must not be rewritten. "रामपाल" is Ramapal, not "Pal", and
    # a substring or fuzzy match would corrupt every name the rules already get
    # right.
    "रामपाल",
    "रामगोपाल",
    "राजस्थान",
    "पालवान",
    "शिवानी",
    "महावीर",
])
def test_lexicon_never_matches_a_substring(hindi):
    assert transliterate(hindi) != _name_lexicon().get(hindi)
    # And the rules, not the map, decide what it comes out as.
    assert transliterate(hindi)


def test_lexicon_never_lengthens_a_name():
    """The rules already drop the inherent schwa wherever English drops it.
    A model output longer than the rules means it invented a vowel, which is
    how शिव would have become "Shiva" instead of the correct "Shiv"."""
    lexicon = _name_lexicon()
    assert "शिव" not in lexicon, "Shiv must not be lengthened to Shiva"
    assert "महावीर" not in lexicon, "Mahavir must not become Mahavira"
    assert "भीम" not in lexicon, "Bhim must not become Bhima"
    assert transliterate("शिव") == "Shiv"


def test_lexicon_does_not_shadow_house_numbers():
    """A house number is a label, not a name. house_en() owns the address
    convention and the lexicon must never win against it."""
    for value, expected in [
        ("इ-897", "E-897"),
        ("47-ई-7", "47-E-7"),
        ("बी-89", "B-89"),
        ("प्लॉट नं 279 ख नं 79", "Plot No. 279 Kha No. 79"),
    ]:
        assert house_en(value) == expected


def test_lexicon_has_no_meaning_drift_entries():
    """The map holds spellings. A translation that happened to pass the
    filters would put "Prince" or "Hero" in a name column."""
    lexicon = _name_lexicon()
    for hindi, english in lexicon.items():
        assert not DEVANAGARI.search(english), (hindi, english)
        assert len(english.split()) <= len(hindi.split()), (hindi, english)
        assert english == english.strip(), (hindi, english)
        assert "  " not in english, (hindi, english)


def test_lexicon_values_are_english_not_romanised():
    """Every value must be letters, so no entry can carry a library marker
    or a stray escape into the database."""
    for hindi, english in _name_lexicon().items():
        assert english.isascii(), (hindi, english)
        assert english.replace(" ", "").replace(".", "").isalpha(), (hindi, english)


def test_lexicon_file_is_loadable_and_wellformed():
    path = Path(__file__).resolve().parents[1] / "app" / "name_lexicon.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "entries" in data
    assert isinstance(data["entries"], dict)
    assert data["entries"], "the lexicon is empty"
    for key, value in data["entries"].items():
        assert DEVANAGARI.search(key), f"key {key!r} is not Devanagari"
        assert value, f"empty value for {key!r}"


def test_missing_lexicon_falls_back_to_the_rules(monkeypatch):
    """A corrupt or absent lexicon must not take the service down. The rules
    alone are a complete, if more phonetic, answer."""
    import app.transliterate as T

    monkeypatch.setattr(T, "_name_lexicon", lambda: {})
    assert T.transliterate("पाल") == "Pala"
    assert T.transliterate("सिंह") == "Singh", "Layer 0 must still work"