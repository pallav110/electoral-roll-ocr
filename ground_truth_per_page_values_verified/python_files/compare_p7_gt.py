#!/usr/bin/env python3
"""Compare page7_results.json against ground truth."""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (121, "AWX5108170", "राजीव सक्सेना",   "पिता", "शिव नारायण",         "63",  43, "पुरुष", False),
    (122, "AWX5108154", "आरती सक्सेना",    "पति",  "राजीव सक्सेना",      "63",  41, "महिला", False),
    (123, "AWX5110788", "मनीषा कुमारी",    "पिता", "सुनील कुमार",        "63",  24, "महिला", False),
    (124, "AWX0511980", "कमल सक्सेना",     "पिता", "शिव नारायण",         "64",  48, "पुरुष", False),
    (125, "GKM8579039", "पूनम सक्सेना",    "पति",  "कमल सक्सेना",        "64",  47, "महिला", False),
    (126, "AWX4355467", "विशाल",           "पिता", "कमल सिंह",           "64",  26, "पुरुष", False),
    (127, "AWX5340690", "मानसी",           "पिता", "कमल सक्सेना",        "64",  22, "महिला", False),
    (128, "GKM8587834", "उदय सिंह",        "पिता", "सत्य राम सिंह",      "65",  64, "पुरुष", False),
    (129, "AWX3583747", "सुरेन्द्र",        "पिता", "सत्य राम सिंह",      "66",  50, "पुरुष", False),
    (130, "GKM6432553", "राकेश कुमार",     "पिता", "शैपनाथ",             "67",  53, "पुरुष", False),
    (131, "GKM7518707", "प्रमिला",         "पति",  "राकेश कुमार",        "67",  51, "महिला", False),
    (132, "AWX4398798", "रोशन कुमार सिंह", "पिता", "राकेश कुमार चौहान", "67",  31, "पुरुष", False),
    (133, "GKM6432900", "संजीव",           "पिता", "जगदीश",              "68",  52, "पुरुष", False),
    (134, "AWX4353132", "पूजा",            "पति",  "अशोक कुमार",         "68",  36, "महिला", False),
    (135, "GKM6433080", "नरेन्द्र",         "पिता", "पूरन सिंह",           "71",  50, "पुरुष", False),
    (136, "GKM7543556", "रामविलास",        "पिता", "रामगोपाल",           "74",  54, "पुरुष", False),
    (137, "GKM5932553", "योगेन्द्र",         "पिता", "चन्दर सिंह",          "74",  52, "पुरुष", False),
    (138, "GKM8062440", "नीतू",            "पति",  "योगेन्द्र",            "74",  50, "महिला", False),
    (139, "AWX1015098", "विनिता",          "पति",  "रामविलास",            "74",  42, "महिला", False),
    (140, "GKM6432918", "संदीप शर्मा",     "पिता", "छोटेलाल शर्मा",      "76",  46, "पुरुष", False),
    (141, "GKM7768609", "निर्मल",          "पति",  "संदीप शर्मा",         "76",  45, "महिला", False),
    (142, "GKM8920027", "कैलासी",          "पति",  "आराम सिंह",           "78",  50, "महिला", False),
    (143, "AWX2532646", "आराम सिंह",       "पिता", "अक्षरपी लाल",         "78",  48, "पुरुष", False),
    (144, "GKM8582926", "देवेन्द्र",         "पिता", "अक्षरपी लाल",         "78",  40, "पुरुष", False),
    (145, "AWX1057223", "ऋषिपाल",          "पिता", "अक्षरपी लाल",         "78",  40, "पुरुष", False),
    (146, "AWX5307822", "राजेश कुमार",     "पिता", "आराम सिंह",           "78",  23, "पुरुष", False),
    (147, "GKM7768617", "नीलम",            "पति",  "राजकुमार",            "79",  52, "महिला", False),
    (148, "GKM5932660", "सुनीता देवी",     "पति",  "अमरेश सिंह",          "80",  54, "महिला", False),
    (149, "GKM8591539", "राकेश",           "पिता", "उमा शंकर",            "80",  50, "पुरुष", False),
    (150, "GKM5932686", "बबीता",           "पति",  "राकेश",                "80",  48, "महिला", False),
]

data = json.loads((_JSON / "page7_results.json").read_text(encoding="utf-8"))
records = {r["sno"] + 120: r for r in data["records"]}

lines = []
mismatches = []


def ocr_fullname(r, rel):
    if rel == "पिता":
        parts = [r.get("voter_father_first_name", ""), r.get("voter_father_middle_name", ""), r.get("voter_father_last_name", "")]
    elif rel == "पति":
        parts = [r.get("voter_husband_first_name", ""), r.get("voter_husband_middle_name", ""), r.get("voter_husband_last_name", "")]
    elif rel == "अन्य":
        parts = [r.get("voter_other_first_name", ""), r.get("voter_other_middle_name", ""), r.get("voter_other_last_name", "")]
    elif rel == "माता":
        parts = [r.get("voter_mother_first_name", ""), r.get("voter_mother_middle_name", ""), r.get("voter_mother_last_name", "")]
    else:
        return ""
    return " ".join(p for p in parts if p).strip()


def ocr_fullvoter(r):
    parts = [r.get("voter_first_name", ""), r.get("voter_middle_name", ""), r.get("voter_sur_name", "")]
    return " ".join(p for p in parts if p).strip()


for sno, epic, name, rel, rel_name, house, age, gender, deleted in GROUND_TRUTH:
    r = records.get(sno)
    row_issues = []

    if r is None:
        lines.append(f"[{sno:3d}] MISSING RECORD")
        mismatches.append((sno, "MISSING"))
        continue

    ocr_epic = r.get("id_card_no", "")
    if ocr_epic != epic:
        row_issues.append(f"EPIC: got={ocr_epic!r} want={epic!r}")

    if deleted:
        if not r.get("is_deleted", False):
            row_issues.append("is_deleted: got=False want=True")
        if row_issues:
            lines.append(f"[{sno:3d}] {epic} MISMATCH:")
            for issue in row_issues:
                lines.append(f"      {issue}")
            mismatches.extend([(sno, i) for i in row_issues])
        else:
            lines.append(f"[{sno:3d}] {epic} OK")
        continue

    ocr_name = ocr_fullvoter(r)
    if ocr_name != name:
        row_issues.append(f"name: got={ocr_name!r} want={name!r}")

    ocr_rel = r.get("relation_name", "")
    if ocr_rel != rel:
        row_issues.append(f"relation_type: got={ocr_rel!r} want={rel!r}")

    ocr_rname = ocr_fullname(r, rel)
    if ocr_rname != rel_name:
        row_issues.append(f"relation_name: got={ocr_rname!r} want={rel_name!r}")

    ocr_house = r.get("house_no", "")
    if ocr_house != house:
        row_issues.append(f"house_no: got={ocr_house!r} want={house!r}")

    ocr_age = str(r.get("age", ""))
    if ocr_age != str(age):
        row_issues.append(f"age: got={ocr_age!r} want={str(age)!r}")

    ocr_gender = r.get("gender", "")
    if ocr_gender != gender:
        row_issues.append(f"gender: got={ocr_gender!r} want={gender!r}")

    if row_issues:
        lines.append(f"[{sno:3d}] {epic} MISMATCH:")
        for issue in row_issues:
            lines.append(f"      {issue}")
        mismatches.extend([(sno, i) for i in row_issues])
    else:
        lines.append(f"[{sno:3d}] {epic} OK")

lines.append("")
lines.append(f"Total mismatched records: {len(set(m[0] for m in mismatches))}/30")
lines.append(f"Total field mismatches: {len(mismatches)}")

out = _GT / "compare_p7_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/30")
print(f"Field mismatches: {len(mismatches)}")
