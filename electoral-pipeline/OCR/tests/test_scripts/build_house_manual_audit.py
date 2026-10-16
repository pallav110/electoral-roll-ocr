#!/usr/bin/env python3
"""Build the human-reviewed house-number ground truth and OCR comparison."""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "OCR/results/house_number_engine_audit.json"
OUT_JSON = ROOT / "OCR/results/house_number_manual_ground_truth.json"
OUT_CSV = ROOT / "OCR/results/house_number_manual_ground_truth.csv"

manual: dict[int, str] = {}


def put(value: str, *specs: int | tuple[int, int]) -> None:
    for spec in specs:
        if isinstance(spec, int):
            manual[spec] = value
        else:
            for sno in range(spec[0], spec[1] + 1):
                manual[sno] = value


put("", 1)
put("00", (2, 3)); put("1", 4); put("1/1044", 5); put("3", (6, 7)); put("4", 8)
put("5", 9); put("7", (10, 11)); put("7 बी", (12, 14)); put("8", (15, 17))
put("8/24", (18, 19)); put("8/81", 20); put("8/444", 21); put("8/468", 22)
put("8/483", 23); put("8/532", 24); put("8/588", 25); put("8/829", 26)
put("8ई-526", 27); put("9", 28); put("09", (29, 30)); put("9/992", 31)
put("10", (32, 34)); put("10/968", (35, 37)); put("10/1168", (38, 39))
put("11", (40, 44)); put("11/193", (45, 46)); put("13", (47, 48)); put("14", 49)
put("15", (50, 54)); put("15/9", 55); put("15 ए", 56); put("16", (57, 58))
put("17", (59, 60)); put("19", 61); put("22", 62); put("22/8", 63)
put("24/8", (64, 66)); put("25", 67); put("28", (68, 73)); put("31", (74, 75))
put("32", 76); put("35", (77, 78)); put("38", (79, 80)); put("42", 81)
put("43", (82, 85)); put("44", (86, 87)); put("45", (88, 89)); put("46", 90)
put("47", (91, 93)); put("47-ई-7", 94); put("48", (95, 98)); put("49", (99, 100))
put("50", 101); put("51", (102, 104)); put("52", (105, 106)); put("53", (107, 108))
put("55", 109); put("56", (110, 111)); put("57", (112, 113)); put("58/8", 114)
put("60", (115, 116)); put("61", 117); put("62", 118); put("63", (119, 123))
put("64", (124, 127)); put("65", 128); put("66", 129); put("67", (130, 132))
put("68", (133, 134)); put("71", 135); put("74", (136, 139)); put("76", (140, 141))
put("78", (142, 146)); put("79", 147); put("80", (148, 150)); put("81", (151, 152))
put("81/8", 153); put("82", (154, 158)); put("83", 159); put("83/8", 160)
put("85", (161, 163)); put("85/8", 164); put("86", (165, 166)); put("87", (167, 168))
put("90", 169); put("93", (170, 171)); put("94", 172); put("95", 173)
put("98", (174, 175)); put("99", 176); put("100", 177); put("102", (178, 182))
put("103", (183, 187)); put("104", (188, 189)); put("105", (190, 192))
put("107", (193, 195)); put("108", 196); put("109", (197, 199)); put("110", (200, 203))
put("110/9", 204); put("112", (205, 206)); put("114", (207, 210))
put("115", (211, 212)); put("117", (213, 216)); put("118", (217, 219))
put("119", (220, 221)); put("121", (222, 223)); put("122", (224, 228))
put("124", 229); put("128", (230, 233)); put("129", (234, 235)); put("131", (236, 239))
put("132", (240, 246)); put("132/10", 247); put("136", (248, 249)); put("138", (250, 251))
put("139", (252, 253)); put("140", (254, 255)); put("141", 256); put("145", (257, 258))
put("146", 259); put("147", (260, 263)); put("148", (264, 265)); put("150", (266, 268))
put("152", (269, 270)); put("154", (271, 272)); put("155", 273); put("156", (274, 278))
put("159", (279, 280)); put("160", (281, 283)); put("162", 284); put("164", 285)
put("166", (286, 287)); put("167", (288, 291)); put("168", (292, 296))
put("168/10", 297); put("169", (298, 299)); put("171", (300, 302)); put("172", 303)
put("173", 304); put("174/ई-10", 305); put("178", (306, 307)); put("179", (308, 309))
put("180", (310, 311)); put("182", (312, 315)); put("182/10", 316); put("183", 317)
put("184", (318, 319)); put("186", (320, 321)); put("187", (322, 324))
put("188", (325, 326)); put("190", (327, 328)); put("191", 329); put("195", 330)
put("196", 331); put("197", (332, 333)); put("198", (334, 336)); put("200", (337, 341))
put("202", 342); put("203", (343, 345)); put("204", (346, 352)); put("205", 353)
put("208", (354, 356)); put("210", (357, 361)); put("216", (362, 365)); put("218", 366)
put("219", (367, 368)); put("222", (369, 370)); put("224", 371); put("225", 372)
put("226", (373, 375)); put("230", 376); put("233", (377, 378)); put("258", 379)
put("265", (380, 383)); put("302", (384, 385)); put("449/8", (386, 387)); put("486", 388)
put("519", 389); put("531", 390); put("538", 391); put("540", (392, 393)); put("543", 394)
put("555", (395, 397)); put("585", 398); put("597", (399, 400)); put("661/8", 401)
put("672", 402); put("674", 403); put("729", 404); put("764", (405, 406))
put("796", (407, 408)); put("802", 409); put("820", 410); put("821", (411, 412))
put("822", 413); put("824", 414); put("864", 415); put("878", 416)
put("909", (417, 418)); put("910/8/ई", 419); put("934", 420); put("957/83", 421)
put("957/85", 422); put("978", 423); put("983", (424, 426)); put("985", (427, 428))
put("990", 429); put("1020", 430); put("1030", 431); put("1053", (432, 433))
put("1126", 434); put("सी-22", (435, 437)); put("सी-26", 438); put("इ-8", 439)
put("इ-8/483", 440); put("E-8/496", 441); put("इ-8/525", 442); put("इ-8/526", 443)
put("इ-8/535", (444, 448)); put("इ-8/538", 449); put("इ-8/540", (450, 452))
put("इ-8/543", (453, 454)); put("इ-8/547", 455); put("इ-8/1172", 456)
put("इ-8-1172", 457); put("इ-9/104", 458); put("इ-9/576", 459)
put("इ-9/579", (460, 461)); put("इ-9/583", 462); put("इ-9/596", (463, 465))
put("इ-9/827", 466); put("इ-9/994", 467); put("इ-9/1030", 468)
put("इ-10/615", (469, 470)); put("इ-508/8", (471, 472)); put("E-532", 473)
put("इ-799", 474); put("इ-817 बी", 475); put("इ-910/8", 476); put("इ-978", 477)
put("इ-985", 478); put("इ-987", (479, 480)); put("इ-1160", (481, 482))
put("इ/8 486", (483, 484)); put("जी-119", 485); put("एच.नं-990", 486)
put("एच.नं-1030", 487); put("एच.नं-1160", 488); put("E-854", 489)
put("265", 490); put("278", 491); put("1153", 492); put("जी-1172", 493)
put("1242", 494); put("122", 495); put("102", 496); put("इ-10/602", 497); put("444", (498, 499)); put("102", 500); put("इ-481", 501)
put("444", 502); put("इ-8,496", (503, 504)); put("574", 505); put("864", 506)
put("574", (507, 508)); put("864", 509); put("574", 510); put("496", 511)
put("574", 512); put("इ-889", 513); put("इ-897", 514); put("इ-857", 515)
put("857", 516); put("इ-85", 517); put("121", 518); put("102", 519)
put("ए-73", 520); put("315/245", (521, 522)); put("इ-9/827", 523)
put("449/8", 524); put("449/9", 525); put("279", 526); put("बी-89", 527)
put("इ-1/75", 528); put("146", 529); put("09/121", 530); put("190", 531)

status = {1: "ambiguous"}
notes = {
    1: "Deletion watermark makes the printed house value unrecoverable without guessing",
    8: "Both engines missed visible value 4", 40: "Both engines missed visible value 11",
    75: "Selected OCR dropped leading 1", 390: "Deletion watermark crosses value; 531 remains legible",
    458: "Printed line continues with locality text after the house value",
    474: "Printed line continues with gali number after the house value",
    515: "Printed line continues with gali number after the house value",
    526: "Printed line also contains khasra number 79",
    531: "Printed line also contains khasra number 701",
}


def number_core(value: str) -> str:
    value = str(value or "").translate(str.maketrans("०१२३४५६७८९", "0123456789"))
    value = re.sub(r"(?<=\d)[, ](?=\d)", "/", value)
    slash = re.search(r"\d{1,3}(?:\s*/\s*\d+)+", value)
    if slash:
        return re.sub(r"\s+", "", slash.group(0))
    groups = re.findall(r"\d+", value)
    return max(groups, key=len) if groups else ""


def full_value(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.translate(str.maketrans("०१२३४५६७८९–—,।", "0123456789--/."))
    return re.sub(r"\s+", "", value).replace(".", "")


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    records = source["records"]
    missing = sorted(set(range(1, len(records) + 1)) - manual.keys())
    extra = sorted(manual.keys() - set(range(1, len(records) + 1)))
    assert not missing and not extra, (missing, extra)
    output = []
    for row in records:
        sno = int(row["sno"])
        truth = manual[sno]
        state = status.get(sno, "clear")
        eligible = state == "clear" and bool(number_core(truth))
        enriched = dict(row)
        enriched.update({
            "manual_house": truth,
            "manual_status": state,
            "manual_note": notes.get(sno, ""),
            "tesseract_matches_manual": eligible and number_core(row["tesseract_house"]) == number_core(truth),
            "paddle_matches_manual": eligible and number_core(row["paddle_house"]) == number_core(truth),
            "selected_matches_manual": eligible and number_core(row["selected_house"]) == number_core(truth),
            "tesseract_full_value_matches_manual": eligible and full_value(row["tesseract_house"]) == full_value(truth),
            "paddle_full_value_matches_manual": eligible and full_value(row["paddle_house"]) == full_value(truth),
            "selected_full_value_matches_manual": eligible and full_value(row["selected_house"]) == full_value(truth),
        })
        output.append(enriched)

    eligible_rows = [row for row in output if row["manual_status"] == "clear" and number_core(row["manual_house"])]
    summary = {
        "records": len(output),
        "clear_ground_truth": len(eligible_rows),
        "ambiguous": sum(row["manual_status"] != "clear" for row in output),
        "numeric_core_matches": {
            "tesseract": sum(row["tesseract_matches_manual"] for row in eligible_rows),
            "paddle": sum(row["paddle_matches_manual"] for row in eligible_rows),
            "selected": sum(row["selected_matches_manual"] for row in eligible_rows),
        },
        "full_value_matches": {
            "tesseract": sum(row["tesseract_full_value_matches_manual"] for row in eligible_rows),
            "paddle": sum(row["paddle_full_value_matches_manual"] for row in eligible_rows),
            "selected": sum(row["selected_full_value_matches_manual"] for row in eligible_rows),
        },
        "status_counts": dict(Counter(row["manual_status"] for row in output)),
    }
    OUT_JSON.write_text(json.dumps({"summary": summary, "records": output}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = list(output[0].keys())
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in output:
            row = dict(row)
            row["focused_tesseract_candidates"] = "|".join(row["focused_tesseract_candidates"])
            row["review_reasons"] = "|".join(row["review_reasons"])
            writer.writerow(row)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
