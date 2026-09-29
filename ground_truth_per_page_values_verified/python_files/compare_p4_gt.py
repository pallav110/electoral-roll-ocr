#!/usr/bin/env python3
"""Compare page4_results.json against ground truth and write diff to compare_p4_results.txt"""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (31, "AWX5001649", "प्रवेश शर्मा",  "पिता", "महेश चंदा शर्मा",     "9/992",  25, "पुरुष", False),
    (32, "GKM8569642", "मनीष कुमार",    "पिता", "विजेन्द्र त्यागी",    "10",     51, "पुरुष", False),
    (33, "GKM5931902", "बबीता",          "पति",  "मुनीश कुमार",         "10",     50, "महिला", False),
    (34, "AWX3583671", "नीरज त्यागी",   "पिता", "विजेन्द्र त्यागी",    "10",     42, "पुरुष", False),
    (35, "AWX2455780", "दीपक सिंह",     "पिता", "तीरपाल",              "10/968", 36, "पुरुष", False),
    (36, "AWX2455806", "राहुल कुमार",   "पिता", "तीरपाल",              "10/968", 36, "पुरुष", False),
    (37, "AWX2455798", "सन्ती तोमर",    "पिता", "वीरपाल",              "10/968", 33, "पुरुष", False),
    (38, "AWX3158094", "आनन्द शर्मा",   "पिता", "महावीर प्रसाद शर्मा", "10/1168",50, "पुरुष", False),
    (39, "AWX3158060", "अभिलाषा शर्मा", "पति",  "आनन्द शर्मा",         "10/1168",46, "महिला", False),
    (40, "GKM2698926", "वी.के.तोमर",    "पिता", "भवर सिंह",            "11",     72, "पुरुष", False),
    (41, "GKM5385259", "कमलेश देवी",    "पति",  "वी.के.तोमर",          "11",     68, "महिला", False),
    (42, "AWX1551134", "पखराज",          "पिता", "ताहर सिंह",           "11",     41, "पुरुष", False),
    (43, "GKM2699007", "संदीप कुमार",   "पिता", "वी.के.तोमर",          "11",     41, "पुरुष", False),
    (44, "GKM8594558", "प्रदीप कुमार",  "पिता", "वी.के.तोमर",          "11",     40, "पुरुष", False),
    (45, "AWX3157955", "अक्षय कुमार",   "पिता", "सुनील कुमार",         "11/193", 31, "पुरुष", False),
    (46, "AWX3157971", "उत्तम कुमार",   "पिता", "सतेंद्र पाल सिंह",   "11/193", 30, "पुरुष", False),
    (47, "GKM5931977", "सुरेश कुमार",   "पिता", "काली चरण",            "13",     57, "पुरुष", False),
    (48, "GKM5931985", "रूपा देवी",     "पति",  "सुरेश कुमार",         "13",     52, "महिला", False),
    (49, "GKM8574642", "गुड्डी",         "पति",  "भीम सिंह",            "14",     42, "महिला", False),
    (50, "GKM5931993", "रघुनाथ",         "पिता", "श्रीचन्द्र",          "15",     68, "पुरुष", False),
    (51, "GKM5932009", "सन्तो",          "पति",  "रघुनाथ",              "15",     64, "महिला", False),
    (52, "AWX0511964", "मीरा देवी",     "पति",  "चन्द्रभान सिंह",     "15",     47, "महिला", False),
    (53, "GKM8062259", "संजय",           "पिता", "रघुनाथ",              "15",     42, "पुरुष", False),
    (54, "AWX0511956", "अरविन्द कुमार", "पिता", "चन्द्रभान सिंह",     "15",     39, "पुरुष", False),
    (55, "AWX3158102", "प्रसून दत्त",   "पिता", "रामपाल",              "15/9",   41, "पुरुष", False),
    (56, "AWX2455871", "चन्द्रभान",     "पिता", "ज्योति प्रसाद",       "15 ए",   55, "पुरुष", False),
    (57, "AWX2531978", "पुष्पा",         "पति",  "प्रमोद शर्मा",        "16",     48, "महिला", False),
    (58, "GKM5932033", "रोशन लाल",      "पिता", "जीवाराम",             "16",     42, "पुरुष", False),
    (59, "GKM6432827", "रवि",            "पिता", "ईश्वर सिंह",          "17",     47, "पुरुष", False),
    (60, "GKM5932041", "सरिता",          "पति",  "रवि",                 "17",     46, "महिला", False),
]

data = json.loads((_JSON / "page4_results.json").read_text(encoding="utf-8"))
# sno resets to 1-30 per request; GT uses 31-60. Map by position.
records = {r["sno"] + 30: r for r in data["records"]}

lines = []
mismatches = []

def ocr_fullname(r, rel):
    if rel == "पिता":
        parts = [r.get("voter_father_first_name",""), r.get("voter_father_middle_name",""), r.get("voter_father_last_name","")]
    elif rel == "पति":
        parts = [r.get("voter_husband_first_name",""), r.get("voter_husband_middle_name",""), r.get("voter_husband_last_name","")]
    elif rel == "अन्य":
        parts = [r.get("voter_other_first_name",""), r.get("voter_other_middle_name",""), r.get("voter_other_last_name","")]
    elif rel == "माता":
        parts = [r.get("voter_mother_first_name",""), r.get("voter_mother_middle_name",""), r.get("voter_mother_last_name","")]
    else:
        return ""
    return " ".join(p for p in parts if p).strip()

def ocr_fullvoter(r):
    parts = [r.get("voter_first_name",""), r.get("voter_middle_name",""), r.get("voter_sur_name","")]
    return " ".join(p for p in parts if p).strip()

for sno, epic, name, rel, rel_name, house, age, gender, deleted in GROUND_TRUTH:
    r = records.get(sno)
    row_issues = []

    if r is None:
        lines.append(f"[{sno:2d}] MISSING RECORD")
        mismatches.append((sno, "MISSING"))
        continue

    ocr_epic = r.get("id_card_no", "")
    if ocr_epic != epic:
        row_issues.append(f"EPIC: got={ocr_epic!r} want={epic!r}")

    if deleted:
        if not r.get("is_deleted", False):
            row_issues.append(f"is_deleted: got=False want=True")
        if row_issues:
            lines.append(f"[{sno:2d}] {epic} MISMATCH:")
            for issue in row_issues:
                lines.append(f"      {issue}")
            mismatches.extend([(sno, i) for i in row_issues])
        else:
            lines.append(f"[{sno:2d}] {epic} OK")
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
        lines.append(f"[{sno:2d}] {epic} MISMATCH:")
        for issue in row_issues:
            lines.append(f"      {issue}")
        mismatches.extend([(sno, i) for i in row_issues])
    else:
        lines.append(f"[{sno:2d}] {epic} OK")

lines.append("")
lines.append(f"Total mismatched records: {len(set(m[0] for m in mismatches))}/30")
lines.append(f"Total field mismatches: {len(mismatches)}")

out = _GT / "compare_p4_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/30")
print(f"Field mismatches: {len(mismatches)}")
