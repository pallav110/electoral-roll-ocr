#!/usr/bin/env python3
"""Compare page6_results.json against ground truth."""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (91,  "GKM8572026", "ओमपाल सिंह",        "पिता", "गुपड़ सिंह",          "47",     66, "पुरुष", False),
    (92,  "GKM6432876", "रविन्द्र",           "पिता", "गुपड़ सिंह",          "47",     59, "पुरुष", False),
    (93,  "GKM6432868", "राजीव",             "पिता", "गुपड़ सिंह",          "47",     59, "पुरुष", False),
    (94,  "AWX3717220", "नीरज कुमार",        "पिता", "वीरेन्द्र प्रसाद",   "47-ई-7", 30, "पुरुष", False),
    (95,  "AWX2243483", "तेजपाल सिंह",       "पिता", "रामस्वरूप सिंह",     "48",     58, "पुरुष", False),
    (96,  "AWX2243467", "सर्वेश देवी",       "पति",  "तेजपाल सिंह",        "48",     55, "महिला", False),
    (97,  "GKM6453716", "सावित्री देवी",     "पति",  "आशीष कुमार",         "48",     48, "महिला", False),
    (98,  "GKM6432744", "आशीष कुमार",       "पिता", "उदय सिंह",           "48",     42, "पुरुष", False),
    (99,  "GKM7768583", "ओमवीर सिंह",        "पिता", "गंगू राम",            "49",     60, "पुरुष", False),
    (100, "GKM5932371", "विमला",             "पति",  "ओमवीर",              "49",     54, "महिला", False),
    (101, "GKM5932389", "सोहन बैरी",         "पति",  "ओमवीर",              "50",     58, "महिला", False),
    (102, "GKM5932421", "सतपाल",             "पिता", "प्रीतम सिंह देवी",    "51",     45, "पुरुष", False),
    (103, "GKM5932413", "मांगीराम",          "पिता", "चौधरी प्रीतम सिंह",  "51",     45, "पुरुष", False),
    (104, "GKM5932405", "विमला देवी",        "पति",  "मांगे राम",           "51",     44, "महिला", False),
    (105, "GKM6453732", "सुधा",              "पति",  "वीर सेन",             "52",     50, "महिला", False),
    (106, "AWX2532612", "नीतू शर्मा",        "पति",  "अजय शर्मा",           "52",     49, "महिला", False),
    (107, "GKM5932439", "सरोजबाला",          "पति",  "विनोद कुमार",         "53",     65, "महिला", False),
    (108, "GKM5932447", "विनोद कुमार",       "पिता", "जगवीर सिंह",          "53",     64, "पुरुष", False),
    (109, "GKM5932462", "चंद पाल",           "पिता", "रतन",                 "55",     62, "पुरुष", False),
    (110, "GKM8556086", "मनोज",              "पिता", "महक सिंह",            "56",     45, "पुरुष", False),
    (111, "GKM8556177", "छमा",               "पति",  "मनोज",                "56",     43, "महिला", False),
    (112, "GKM8556185", "जितेन्द्र",          "पिता", "महक सिंह",            "57",     43, "पुरुष", False),
    (113, "GKM8596843", "डोली",              "पति",  "जितेन्द्र",           "57",     42, "महिला", False),
    (114, "AWX3158052", "अजय शर्मा",          "पिता", "रघुराज प्रसाद",      "58/8",   48, "पुरुष", False),
    (115, "GKM5385309", "ज्ञान चन्द्र शर्मा",  "पिता", "ईश्वर चन्द्र",        "60",     59, "पुरुष", False),
    (116, "GKM6453641", "ऊषा शर्मा",         "पति",  "ज्ञान चन्द्र शर्मा",   "60",     58, "महिला", False),
    (117, "GKM5932520", "रजनी",              "पति",  "किशन कुमार",          "61",     54, "महिला", False),
    (118, "GKM6453724", "निविता शर्मा",      "पति",  "हरी शंकर शर्मा",      "62",     57, "महिला", False),
    (119, "GKM8579021", "सुनील सक्सेना",     "पिता", "शिव नारायण",         "63",     52, "पुरुष", False),
    (120, "GKM8579013", "पूनम",              "पति",  "सुनील सक्सेना",       "63",     50, "महिला", False),
]

data = json.loads((_JSON / "page6_results.json").read_text(encoding="utf-8"))
records = {r["sno"] + 90: r for r in data["records"]}

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

out = _GT / "compare_p6_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/30")
print(f"Field mismatches: {len(mismatches)}")
