"""The cleanup layer is keyed on ITRANS, so the default scheme must be ITRANS.

These tests pin the contract between two things that are easy to change
independently: the romanisation scheme _scheme() picks, and the digraph folds
in _clean_segment plus the GLOBAL_STRUCTURAL_MAP keys.

The failure they guard against is silent. With the IAST default, every digraph
fold (shh->sh, rri->ri, amch->anch) became a no-op and the IAST diacritics were
stripped, so शिव came out "Siva" and चन्द्र came out "Candra" -- no exception,
no warning, just a wrong name in an English column.
"""
import pytest

from app.transliterate import (
    _assimilate_anusvara,
    _scheme,
    house_en,
    transliterate,
)


def test_default_scheme_is_itrans():
    """The cleanup rules are written against ITRANS spellings."""
    assert _scheme()[0] == "itrans"


@pytest.mark.parametrize("hindi,expected", [
    # 'sh' and 'ch' are lost when IAST diacritics are stripped. ITRANS spells
    # them as digraphs, which the cleanup layer can actually see.
    ("शिव", "Shiv"),
    ("चन्द्र", "Chandra"),
    ("क्षेत्र", "Kshetra"),
    ("अर्चना", "Archana"),
    ("अक्षय", "Akshay"),
    ("कृष्णा", "Krishna"),
    ("अशोक", "Ashok"),
    ("आशीष", "Ashish"),
    ("जोशी", "Joshi"),
    ("रविन्द्र", "Ravindra"),
])
def test_conjuncts_keep_their_letter_pairs(hindi, expected):
    assert transliterate(hindi) == expected


@pytest.mark.parametrize("hindi,expected", [
    # ज्ञ is ज् + ञ. The library marks the nasal with a literal '~' ("j~na"),
    # which is not a combining mark, so it reached output as "Jnan". The
    # conjunct is fixed as a grapheme, not per word: every compound of ज्ञ is a
    # different string and a map entry could only ever name one of them.
    ("ज्ञ", "Gyan"),
    ("ज्ञान", "Gyan"),
    ("ज्ञानेश", "Gyanesh"),
    ("ज्ञाना", "Gyana"),
    # जन is ज + न, a different letter pair. The rule must not touch it.
    ("जन", "Jana"),
    ("जन्म", "Janma"),
])
def test_gya_conjunct_folds_without_harming_jn(hindi, expected):
    assert transliterate(hindi) == expected


@pytest.mark.parametrize("hindi,expected", [
    # ॑ / ॒ are an OCR misread of the anusvara in नं. ITRANS writes them as
    # the literal two-character escape "\'", which is not a Unicode combining
    # mark, so the mark-stripper cannot remove it and the address came out as
    # "Haus Na\' - 121".
    ("हाऊस न॑ - 121", "Haus No. - 121"),
    ("इ-857 गली न॑.8", "I-857 Gali No..8"),
    ("न॑", "No."),
])
def test_stress_signs_do_not_leak_escapes(hindi, expected):
    assert transliterate(hindi) == expected


@pytest.mark.parametrize("hindi,expected", [
    ("इ-897", "E-897"),
    ("47-ई-7", "47-E-7"),
    ("बी-89", "B-89"),
    ("15 ए", "15 E"),
    ("प्लॉट नं 279 ख नं 79", "Plot No. 279 Kha No. 79"),
])
def test_house_number_conventions_still_hold(hindi, expected):
    """The scheme change must not disturb the address conventions."""
    assert house_en(hindi) == expected


def test_echaenao_regression_is_fixed():
    """एचएनओ is the plan-file case: IAST spells it 'ecaenao', which matches
    no map entry, so it rendered as 'Ecaenao 146'. ITRANS spells it
    'echaenao', which GLOBAL_STRUCTURAL_MAP has always listed as 'HNO'."""
    assert transliterate("एचएनओ 146") == "HNO 146"


@pytest.mark.parametrize("hindi,expected", [
    # Homorganic nasal assimilation. ITRANS writes the anusvara as a literal
    # "M" (सिंह -> "siMha"); the schwa rule then saw a bare consonant, dropped
    # the inherent 'a', and the M went with it. Every one of the 22 anusvara
    # names in the roll was wrong as a result -- सिंह alone is 65 occurrences.
    # The nasal takes the class of the consonant that follows it.
    ("सिंह", "Singh"),        # si + n + ha, velar -> ng + h
    ("हंस", "Hans"),          # ha + n + sa, dental
    ("शंकर", "Shankar"),      # sha + n + ka, velar
    ("पंकज", "Pankaj"),      # pa + n + ka, velar
    ("गंगू", "Gangu"),        # ga + n + gu, velar
    ("चंद", "Chand"),        # cha + n + da, retroflex
    ("पंडित", "Pandit"),      # pa + n + Dita, retroflex
    ("बिंदी", "Bindi"),       # bi + n + di, dental
    ("मंजू", "Manju"),        # ma + n + ju, palatal
    ("रंग", "Rang"),          # ra + n + ga, velar
    ("अंकुर", "Ankur"),       # a + n + kura, velar
    ("धर्मेंद्र", "Dharmendra"),
    ("प्रियंका", "Priyanka"),
    ("प्रभांशु", "Prabhanshu"),
    ("मांगीराम", "Mangiram"),
    ("गंगाशरण", "Gangasharan"),
    # A word-final anusvara has nothing to join, so it is left alone. Rewriting
    # it as a bare न invents a syllable: संत became "Sanat", not "Sant".
    ("संत", "Sant"),
])
def test_anusvara_assimilates_to_the_following_consonant(hindi, expected):
    assert transliterate(hindi) == expected


@pytest.mark.parametrize("hindi,expected", [
    # नं is the abbreviation for "number" and GLOBAL_STRUCTURAL_MAP reads it as
    # "No.". Assimilating it broke that in two ways at once: as a word-final
    # anusvara it became नन -> "Nana", and inside "नं-बी" the hyphen looked like
    # a word boundary and it became "Nan-B". The token has to survive intact.
    ("नं", "No."),
    ("न॑", "No."),
    ("प्लॉट नं 279 ख नं 79", "Plot No. 279 Kha No. 79"),
    ("पी. नं-बी 190, ख नं-701", "P. No.-B 190, Kha No.-701"),
    ("इ-857 गली न॑.8", "I-857 Gali No..8"),
])
def test_anusvara_rule_does_not_break_the_number_abbreviation(hindi, expected):
    assert transliterate(hindi) == expected


def test_anusvara_rule_does_not_break_the_number_abbreviation_in_a_house_number():
    """Same abbreviation, but through house_en() -- which is the function
    normalize.py actually calls for this field."""
    assert house_en("हाऊस नं इ5/245") == "Haus No. E5/245"


def test_assimilate_anusvara_never_inserts_a_latin_letter():
    """An earlier version mapped the nasal classes to ASCII 'n'/'N'/'m',
    which spliced a Latin character into Devanagari text -- the same class of
    bug _normalize_cyrillic exists to clean up afterwards."""
    for token in ["सिंह", "शंकर", "चंद", "पंडित", "बिंदी", "मंजू", "संजीव"]:
        out = _assimilate_anusvara(token)
        assert all(ord(ch) >= 0x0900 or ch.isspace() for ch in out), (token, out)


def test_scheme_env_override_still_works(monkeypatch):
    """An explicit TRANSLITERATION_SCHEME must still be honoured -- but the
    IAST form is known to lose 'sh'/'ch', so this pins the *override*, not
    the output quality."""
    monkeypatch.setenv("TRANSLITERATION_SCHEME", "iast")
    assert _scheme() == ("iast", False)
    monkeypatch.setenv("TRANSLITERATION_SCHEME", "not-a-scheme")
    assert _scheme()[0] == "itrans"  # unknown scheme falls back to the default