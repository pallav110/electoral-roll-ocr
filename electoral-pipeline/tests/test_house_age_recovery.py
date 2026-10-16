"""Safe house and age recovery. A printed dash is not a digit, and a split age is not a guess."""
import numpy as np

import ocr_pdf_api as ocr


def _card(width=420, height=220):
    return np.full((height, width, 3), 255, np.uint8)


def _paint_dash(card):
    height, width = card.shape[:2]
    x0 = int(width * 0.25)
    y = int(height * 0.52)
    card[y:y + 3, x0:x0 + 8] = 0
    return card


def _paint_digit_column(card):
    height, width = card.shape[:2]
    x0 = int(width * 0.32)
    y0 = int(height * 0.47)
    card[y0:y0 + 16, x0:x0 + 3] = 0
    return card


def test_isolated_dash_is_not_a_house_number():
    assert ocr._detect_house_not_provided(_paint_dash(_card())) is True


def test_a_tall_glyph_is_not_called_a_dash():
    assert ocr._detect_house_not_provided(_paint_digit_column(_card())) is False


def test_an_empty_house_crop_is_not_called_a_dash():
    assert ocr._detect_house_not_provided(_card()) is False


def test_single_digit_from_a_dash_is_rejected():
    ocr.recovery_stats_reset()
    kind, value = ocr._arbitrate_house_reads(True, ["7", "7"])
    assert kind == "not_provided_in_roll"
    assert value == "-"
    assert value not in {"1", "7", "8", "9"}
    assert ocr.recovery_stats_snapshot()["house_false_digit_blocked"] == 1


def test_a_long_hallucination_does_not_override_a_dash():
    kind, value = ocr._arbitrate_house_reads(True, ["7257"])
    assert kind == "not_provided_in_roll"
    assert value == "-"


def test_two_readers_keep_a_real_number_and_a_slash():
    assert ocr._arbitrate_house_reads(False, ["27", "27"]) == ("value_recovered", "27")
    assert ocr._arbitrate_house_reads(False, ["8/774", "8/774"]) == ("value_recovered", "8/774")


def test_one_reader_and_a_blank_crop_stay_unresolved():
    assert ocr._arbitrate_house_reads(False, ["7"]) == ("unresolved", "")
    # Two variants of the same tiny crop can make the same false digit from
    # a printed dash. The direct-card fallback must fail closed here.
    assert ocr._arbitrate_house_reads(False, ["7", "7"]) == ("unresolved", "")
    assert ocr._arbitrate_house_reads(False, []) == ("unresolved", "")


def test_hindi_address_needs_two_agreeing_reads():
    text = "विष्णु नगर"
    assert ocr._arbitrate_house_reads(False, [text]) == ("unresolved", "")
    assert ocr._arbitrate_house_reads(False, [text, text]) == ("value_recovered", text)


def test_existing_house_and_age_are_not_touched():
    record = {"house_no": "8/774", "age": "41", "_field_sources": {}}
    ocr._recover_missing_house_and_age_from_direct_card(None, None, record)
    assert record["house_no"] == "8/774"
    assert record["age"] == "41"
    assert record["_field_sources"] == {}


def test_age_19_is_not_rewritten_as_49():
    assert ocr._arbitrate_age_reads(["19", "49"]) == ("unresolved", "")
    assert ocr._arbitrate_age_reads(["19", "19"]) == ("value_recovered", "19")


def test_age_51_is_not_rewritten_as_54():
    assert ocr._arbitrate_age_reads(["51", "54"]) == ("unresolved", "")
    assert ocr._arbitrate_age_reads(["51", "51"]) == ("value_recovered", "51")


def test_clear_ages_and_a_lone_read():
    for age in ("18", "21", "71"):
        assert ocr._arbitrate_age_reads([age, age]) == ("value_recovered", age)
    assert ocr._arbitrate_age_reads(["19"]) == ("unresolved", "")
    assert ocr._arbitrate_age_reads([]) == ("unresolved", "")


def test_a_tight_crop_can_confirm_an_age_and_cannot_break_a_tie():
    assert ocr._arbitrate_age_reads(["51"], tight="51") == ("value_recovered", "51")
    assert ocr._arbitrate_age_reads([], tight="21") == ("value_recovered", "21")
    assert ocr._arbitrate_age_reads(["19"], tight="49") == ("unresolved", "")
    assert ocr._arbitrate_age_reads(["51", "54"], tight="51") == ("unresolved", "")


def test_english_house_words_are_not_stored_as_symbol_soup():
    soup = "24 1(1+578182 1541418&4"
    assert ocr._house_is_symbol_soup(soup) is True
    assert ocr._house_is_symbol_soup("124 KHASRA NUMBER") is False
    assert ocr._house_is_symbol_soup("8/774") is False
    assert ocr._house_is_symbol_soup("23(डी)") is False
    assert ocr._house_is_symbol_soup("विष्णु नगर") is False
    assert ocr._agree_english_house_text(
        "Fem : 1 24} KHASRA NUMBER",
        "124 KHASRA NUMBER",
    ) == "124 KHASRA NUMBER"
    assert ocr._agree_english_house_text(
        "277 B GALI NO-8",
        "277 B GALI NO-8 EE",
    ) == "277 B GALI NO-8"
    assert ocr._agree_english_house_text("KHOLI NO-91", "KHOL NO-91") == ""
    assert ocr._agree_english_house_text("UTTAR PRADESH", "UTTAR PRADESH") == ""


def test_symbol_soup_is_replaced_only_by_an_agreed_line(monkeypatch):
    record = {"house_no": "24 1(1+578182 1541418&4", "_field_sources": {}, "_review_reasons": []}
    monkeypatch.setattr(ocr, "_agreed_literal_house", lambda image: "124 KHASRA NUMBER")
    ocr._replace_symbol_soup_house(object(), record, None)
    assert record["house_no"] == "124 KHASRA NUMBER"
    assert record["_field_sources"]["house_number"] == "english_line_consensus"

    rejected = {"house_no": "24 1(1+578182 1541418&4", "_field_sources": {}, "_review_reasons": []}
    monkeypatch.setattr(ocr, "_agreed_literal_house", lambda image: "")
    ocr._replace_symbol_soup_house(object(), rejected, None)
    assert rejected["house_no"] == ""
    assert "house_symbol_soup_unresolved" in rejected["_review_reasons"]


def test_a_plain_house_number_is_not_sent_through_the_english_reread(monkeypatch):
    record = {"house_no": "27", "_field_sources": {}}
    monkeypatch.setattr(ocr, "_agreed_literal_house", lambda image: (_ for _ in ()).throw(AssertionError("reread")))
    ocr._replace_symbol_soup_house(object(), record, "27")
    assert record["house_no"] == "27"


def test_recovery_records_provenance_without_inventing_a_number(monkeypatch):
    card = _paint_dash(_card())
    monkeypatch.setattr(ocr, "_render_exact_card", lambda page, rect: card)
    monkeypatch.setattr(ocr, "_numeric_house_signals", lambda crop: ["7"])
    monkeypatch.setattr(ocr, "_collect_age_reads", lambda image: ["19", "49"])
    record = {"house_no": "", "age": "", "_field_sources": {}, "_review_reasons": []}
    ocr._recover_missing_house_and_age_from_direct_card(object(), object(), record)
    assert record["house_no"] == "-"
    assert record["age"] == ""
    assert record["_field_sources"]["house_number"] == "source_placeholder_detected"
    assert record["_field_sources"]["age"] == "unresolved_after_direct_card"
    assert "age_direct_readers_disagree" in record["_review_reasons"]
    assert record["house_no"] != "7"


def test_house_tail_keeps_the_printed_form_and_rejects_a_header():
    assert ocr._house_tail_token("ot eat: A 19\nee") == "A 19"
    assert ocr._house_tail_token("a teat: A 19\npp Ct") == "A 19"
    assert ocr._house_tail_token("aa: 301\nee a.") == "301"
    assert ocr._house_tail_token("Ha: 95/11") == "95/11"
    assert ocr._house_tail_token("01") == "01"
    assert ocr._house_tail_token("em: 1") == "1"
    assert ocr._house_tail_token("Uttar Pradesh") == ""
    assert ocr._house_tail_token("a eee 2 171") == "171"
    assert ocr._house_tail_token("ea 141") == "141"
    assert ocr._house_tail_token("eam: 151 ee 0") == "151"
    assert ocr._house_tail_token("124 KHASRA NUMBER") == ""
    assert ocr._house_tail_token("PANCHVATI PART -2") == ""


def test_an_agreed_house_line_fills_a_blank_digit_crop(monkeypatch):
    monkeypatch.setattr(ocr, "_detect_house_not_provided", lambda image: False)
    monkeypatch.setattr(ocr, "_numeric_house_signals", lambda crop: [])
    monkeypatch.setattr(ocr, "_labelled_house_signals", lambda image: [])
    monkeypatch.setattr(ocr, "_agreed_house_tail", lambda image: "281")
    assert ocr._classify_house_on_card(_card()) == ("value_recovered", "281")


def test_one_digit_crop_reading_does_not_veto_the_house_line(monkeypatch):
    monkeypatch.setattr(ocr, "_detect_house_not_provided", lambda image: False)
    monkeypatch.setattr(ocr, "_numeric_house_signals", lambda crop: ["7"])
    monkeypatch.setattr(ocr, "_labelled_house_signals", lambda image: [])
    monkeypatch.setattr(ocr, "_agreed_house_tail", lambda image: "61")
    assert ocr._classify_house_on_card(_card()) == ("value_recovered", "61")


def test_separated_strokes_fill_a_house_the_line_missed(monkeypatch):
    monkeypatch.setattr(ocr, "_detect_house_not_provided", lambda image: False)
    monkeypatch.setattr(ocr, "_numeric_house_signals", lambda crop: [])
    monkeypatch.setattr(ocr, "_labelled_house_signals", lambda image: [])
    monkeypatch.setattr(ocr, "_agreed_house_tail", lambda image: "")
    monkeypatch.setattr(ocr, "_house_from_separated_digits", lambda crop: "181")
    assert ocr._classify_house_on_card(_card()) == ("value_recovered", "181")


def test_a_slash_read_as_an_extra_one_keeps_the_line():
    assert ocr._choose_agreed_house_value("31119", "311/9", "", False) == "311/9"


def test_an_unread_last_stroke_is_not_dropped_down_to_its_prefix():
    assert ocr._choose_agreed_house_value("1+", "", "", False) == ""
    assert ocr._choose_agreed_house_value("33+", "330", "ए", False) == "33 ए"
    assert ocr._choose_agreed_house_value("33+", "330", "", False) == "33"


def test_a_word_on_the_value_line_is_not_reduced_to_digits():
    assert ocr._choose_agreed_house_value("124", "124", "", True) == ""


def test_value_agreement_fills_when_earlier_reads_are_empty(monkeypatch):
    monkeypatch.setattr(ocr, "_detect_house_not_provided", lambda image: False)
    monkeypatch.setattr(ocr, "_numeric_house_signals", lambda crop: [])
    monkeypatch.setattr(ocr, "_labelled_house_signals", lambda image: [])
    monkeypatch.setattr(ocr, "_agreed_house_tail", lambda image: "")
    monkeypatch.setattr(ocr, "_house_from_separated_digits", lambda crop: "")
    monkeypatch.setattr(ocr, "_house_from_value_agreement", lambda crop: "311/9")
    assert ocr._classify_house_on_card(_card()) == ("value_recovered", "311/9")


def test_a_split_digit_crop_does_not_block_the_agreed_line(monkeypatch):
    monkeypatch.setattr(ocr, "_detect_house_not_provided", lambda image: False)
    monkeypatch.setattr(ocr, "_numeric_house_signals", lambda crop: ["191", "1191"])
    monkeypatch.setattr(ocr, "_labelled_house_signals", lambda image: [])
    monkeypatch.setattr(ocr, "_agreed_house_tail", lambda image: "191")
    assert ocr._classify_house_on_card(_card()) == ("value_recovered", "191")


def test_a_recovered_one_joins_the_number_not_the_letter():
    assert ocr._place_recovered_leading_one("ए-08, एफ-03") == "ए-108, एफ-03"
    assert ocr._place_recovered_leading_one("सी/98,60 फ़ुटा रोड") == "सी/198,60 फ़ुटा रोड"
    assert ocr._place_recovered_leading_one("64") == "164"


def test_a_junk_glyph_between_digits_is_dropped():
    assert ocr._strip_house_digit_glue("16व4") == "164"
    assert ocr._strip_house_digit_glue("102य4") == "1024"
    assert ocr._strip_house_digit_glue("16&4-ग") == "164-ग"
    assert ocr._strip_house_digit_glue("180,वार्ड नं 15") == "180,वार्ड नं 15"


def test_telugu_name_is_stored_as_the_same_hindi_word():
    assert ocr._brahmic_to_devanagari("చంద్ర భూషణ్ వర్మ") == "चंद्र भूषण वर्म"
    assert ocr._foreign_name_as_devanagari("पिता का नाम: చంద్ర భూషణ్ వర్మ") == "चंद्र भूषण वर्म"


def test_a_foreign_name_is_kept_only_when_two_reads_agree():
    line = "पिता का नाम: చంద్ర భూషణ్ వర్మ"
    assert ocr._agreed_foreign_name([line, line]) == "चंद्र भूषण वर्म"
    assert ocr._agreed_foreign_name([line, "पिता का नाम: రూప్"]) == ""
    assert ocr._agreed_foreign_name(["पिता का नाम: .", "पिता का नाम:"]) == ""


def test_a_three_digit_age_past_120_is_not_stored():
    record = ocr.parse_voter_box_from_ocr_lines(["आयु : 336 लिंग : पुरुष"])
    assert record.get("age", "") == ""
    kept = ocr.parse_voter_box_from_ocr_lines(["आयु : 36 लिंग : पुरुष"])
    assert kept.get("age") == "36"
