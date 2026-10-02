"""Tests for Devanagari → Latin transliteration and the bilingual record mapping."""
import uuid

import pytest

from app.extractor import _join_name_parts, _join_relation_name, _relation_key
from app.normalize import map_record
from app.transliterate import gender_en, relation_en, source_hash, transliterate


@pytest.mark.parametrize("hindi,expected", [
    ("राजीव", "Rajiv"),
    ("बबीता", "Babita"),
    ("राजीव सक्सेना", "Rajiv Saksena"),
])
def test_transliterate_names(hindi, expected):
    assert transliterate(hindi) == expected


@pytest.mark.parametrize("hindi,expected", [
    # A name ending in मात्रा ा carries a real vowel. Dropping it produced
    # Kalpan, Rekh, Saksen, Sudh - a consonant-final English word that does
    # not spell the Hindi at all.
    ("कल्पना", "Kalpana"),
    ("रेखा", "Rekha"),
    ("सुधा", "Sudha"),
    # सक्सेना used to stand here, but it is now a Layer 1 lexicon key and
    # resolves to "Saxena" -- an English spelling convention, not a schwa
    # decision. This test is about the trailing vowel, so it uses a name the
    # lexicon does not carry; see test_name_lexicon.py for the map.
    ("सरस्वती", "Sarasvati"),
    ("अर्चना", "Archana"),
    # A name ending in a bare consonant carries an inherent schwa that
    # English does not write. Keeping it produced Rohita, Amita, Kumara.
    ("रोहित", "Rohit"),
    ("अमित", "Amit"),
    ("कुमार", "Kumar"),
    # Conjunct endings resolve to a conventional vowel: Ravindra, not Ravindr.
    ("रविन्द्र", "Ravindra"),
    ("चन्द्र", "Chandra"),
])
def test_final_vowel_follows_the_devanagari_not_a_suffix_list(hindi, expected):
    """The trailing-'a' decision is made from the script.

    It used to be made by a hand-written suffix list (ita|ika|iya|ta|da|ma|
    va|la|ra), which cannot work: by that point राजीव and कल्पना have both
    become "rajiva"/"kalpana" and differ only in a vowel quality already
    flattened away. That rule failed in both directions.
    """
    assert transliterate(hindi) == expected


def test_the_rule_cannot_be_defeated_by_a_suffix_heuristic():
    """सुनीता and अमित both end in the romanised syllable 'ta'.

    One must keep the vowel and the other must drop it, so no rule reading
    only the romanised form can decide. These two are the guard against
    someone 'simplifying' this back into a suffix list.
    """
    assert transliterate("सुनीता") == "Sunita"
    assert transliterate("अमित") == "Amit"


@pytest.mark.parametrize("value", ["64", "AWX5108170", "300", "43"])
def test_digits_and_epics_pass_through_untouched(value):
    """A number must never be 'translated'. These are the fields whose
    regressions the pipeline treats as unacceptable, so pass-through is a
    correctness requirement, not a convenience."""
    assert transliterate(value) == value


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_becomes_null_not_empty_string(value):
    """Empty string in a *_en column reads as 'we tried and got nothing';
    NULL correctly says 'there is no English for this'."""
    assert transliterate(value) is None


def test_already_latin_passes_through():
    assert transliterate("Ramesh") == "Ramesh"


def test_gender_and_relation_use_vocabulary_not_phonetics():
    """'purusa'/'pita' are technically correct and practically useless as
    stored values; a roll needs Male/Father."""
    assert gender_en("पुरुष") == "Male"
    assert gender_en("महिला") == "Female"
    assert relation_en("पिता") == "Father"
    assert relation_en("पति") == "Husband"


def test_unknown_gender_falls_back_to_transliteration():
    assert gender_en("कुछ") == "Kuch"


def test_output_is_ascii_only():
    for value in ("राजीव सक्सेना", "उत्तरांचल कालोनी गली न0 8 से 9", "पुरुष"):
        out = transliterate(value)
        assert out is not None
        assert all(ord(ch) < 128 for ch in out), f"non-ascii in {out!r}"


def test_source_hash_is_stable_and_input_sensitive():
    assert source_hash("राजीव", "पिता") == source_hash("राजीव", "पिता")
    assert source_hash("राजीव", "पिता") != source_hash("राजिव", "पिता")


def _ocr_record(**over):
    rec = {
        "voter_first_name": "राजीव", "voter_middle_name": "", "voter_sur_name": "सक्सेना",
        "relation_name": "पिता",
        "voter_father_first_name": "शिव", "voter_father_middle_name": "", "voter_father_last_name": "नारायण",
        "voter_husband_first_name": "आरती", "voter_husband_middle_name": "", "voter_husband_last_name": "सक्सेना",
        "voter_mother_first_name": "", "voter_mother_middle_name": "", "voter_mother_last_name": "",
        "voter_other_first_name": "", "voter_other_middle_name": "", "voter_other_last_name": "",
        "gender": "पुरुष", "age": "43", "id_card_no": "AWX5108170", "house_no": "63",
    }
    rec.update(over)
    return rec


def test_name_parts_are_joined_in_order():
    assert _join_name_parts(_ocr_record(), "voter") == "राजीव सक्सेना"


@pytest.mark.parametrize("relation,expected", [
    ("पिता", "शिव नारायण"),
    ("पति", "आरती सक्सेना"),
])
def test_relation_name_reads_the_group_matching_the_type(relation, expected):
    """All four groups are populated in a real record; picking the wrong one
    silently attaches the wrong relative's name."""
    assert _join_relation_name(_ocr_record(relation_name=relation)) == expected


def test_relation_key_is_english():
    assert _relation_key("पिता") == "father"
    assert _relation_key("पति") == "husband"


def _map(ocr_rec, row=1):
    item = {
        "source": {"page_number": 7, "row_number": row},
        "hindi": {
            "name": _join_name_parts(ocr_rec, "voter"),
            "relative_name": _join_relation_name(ocr_rec),
            "house_number": ocr_rec.get("house_no", ""),
            "gender": ocr_rec.get("gender", ""),
            "section_name": "उत्तरांचल कालोनी",
        },
        "english": {"name": None, "relative_name": None, "house_number": ocr_rec.get("house_no", ""),
                    "gender": None, "section_name": None},
        "common": {"serial_number": 1, "epic_number": ocr_rec.get("id_card_no"),
                   "age": ocr_rec.get("age"), "relationship_type": _relation_key(ocr_rec.get("relation_name"))},
        # raw_record is what map_record reads the Hindi relation label from.
        # It was missing here, so no test ever exercised the field and the
        # column could stay NULL across every row without failing anything.
        "raw_record": dict(ocr_rec),
        "confidence": 0.95,
    }
    return map_record(item, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


def test_record_stores_both_languages():
    r = _map(_ocr_record())
    assert r.name_hi == "राजीव सक्सेना"
    assert r.name_en == "Rajiv Saksena"
    assert r.relative_name_hi == "शिव नारायण"
    assert r.relative_name_en == "Shiv Narayan"
    assert r.gender_hi == "पुरुष" and r.gender_en == "Male"
    assert r.house_number_hi == "63" and r.house_number_en == "63"
    assert r.relationship_type == "father"
    assert r.age == 43 and r.epic_number == "AWX5108170"


@pytest.mark.parametrize("relation", ["पिता", "पति", "माता", "अन्य"])
def test_relation_name_reaches_the_stored_record(relation):
    """The Hindi relation label must survive into the record.

    map_record read this at line 157 but never passed it to the constructor,
    so the column was NULL on every row. relationship_type is derived from
    the same value upstream, so it stayed populated and masked the loss.
    """
    r = _map(_ocr_record(relation_name=relation))
    assert r.relation_name == relation


def test_ocr_fields_missing_from_raw_record_are_not_fabricated():
    """No raw_record means no OCR fields. They must be empty, not invented."""
    item = {
        "source": {"page_number": 7, "row_number": 1},
        "hindi": {"name": "राजीव सक्सेना", "relative_name": "", "house_number": "",
                  "gender": "", "section_name": ""},
        "english": {"name": None, "relative_name": None, "house_number": "", "gender": None,
                    "section_name": None},
        "common": {"serial_number": 1, "epic_number": "AWX5108170", "age": "43"},
        "confidence": 0.95,
    }
    r = map_record(item, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert r.relation_name is None
    assert r.relationship_type is None


def test_stale_english_in_extractor_output_is_ignored():
    """English must be derived from stored Hindi. If the extractor hands us a
    contradictory English value, the Hindi must win."""
    item = {
        "source": {"page_number": 7, "row_number": 1},
        "hindi": {"name": "राजीव", "relative_name": "शिव", "house_number": "63", "gender": "पुरुष"},
        "english": {"name": "WRONG", "relative_name": "ALSO WRONG", "house_number": "999",
                    "gender": "Nonsense"},
        "common": {"serial_number": 1, "epic_number": "X", "age": "43", "relationship_type": "father"},
        "confidence": 0.95,
    }
    r = map_record(item, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert r.name_en == "Rajiv"
    assert r.relative_name_en == "Shiv"
    assert r.gender_en == "Male"


def test_translit_source_hash_is_recorded():
    r = _map(_ocr_record())
    assert r.translit_source_hash == source_hash(
        r.name_hi, r.relative_name_hi, r.gender_hi, r.section_name_hi, r.house_number_hi)


def test_missing_name_is_flagged_invalid():
    rec = _ocr_record(voter_first_name="", voter_middle_name="", voter_sur_name="")
    r = _map(rec)
    assert not r.is_valid
    assert "name missing" in r.validation_errors
