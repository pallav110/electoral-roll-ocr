#!/usr/bin/env python3
"""Compare page5_results.json against ground truth."""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (61, "GKM5932074", "सुधीर",           "पिता", "ब्रह्म पाल",        "19",   47, "पुरुष", False),
    (62, "GKM6432694", "आफिफ",            "पिता", "शाहिद खान",         "22",   44, "पुरुष", False),
    (63, "AWX3156890", "शाहीन",           "पति",  "आफिफ खान",          "22/8", 40, "महिला", False),
    (64, "AWX3157187", "आराम सिंह",       "पिता", "लखपत सिंह",         "24/8", 42, "पुरुष", False),
    (65, "AWX5504428", "उमेश",            "पिता", "आराम सिंह",         "24/8", 23, "पुरुष", False),
    (66, "AWX5499462", "नीतू",            "पति",  "उमेश",              "24/8", 22, "महिला", False),
    (67, "GKM6202915", "प्रदीप कुमार",   "पिता", "सत्येन्द्र सिंह",  "25",   42, "पुरुष", False),
    (68, "GKM8577496", "राजबोरी",         "पति",  "करण सिंह",          "28",   72, "महिला", False),
    (69, "GKM8583965", "सतेंद्र कुमार",  "पिता", "करण सिंह",          "28",   52, "पुरुष", False),
    (70, "GKM8577504", "सीमा देवी",       "पति",  "सतेंद्र कुमार",    "28",   50, "महिला", False),
    (71, "GKM8583973", "संजीव कुमार",    "पिता", "करण सिंह",          "28",   49, "पुरुष", False),
    (72, "GKM8577512", "रवीता",           "पति",  "संजीव",             "28",   46, "महिला", False),
    (73, "AWX4398830", "अमन सिंह",        "पिता", "सतेंद्र कुमार",    "28",   27, "पुरुष", False),
    (74, "GKM6432637", "इन्द्रपाल",      "पिता", "रणवीर",             "31",   42, "पुरुष", False),
    (75, "GKM8594327", "मीना",            "पति",  "प्रदीप कुमार",      "31",   38, "महिला", False),
    (76, "GKM5932223", "कुसुम सिंह",     "पति",  "राजपाल सिंह",       "32",   63, "महिला", False),
    (77, "GKM5932272", "सुरेश",           "पति",  "करण पाल",           "35",   52, "महिला", False),
    (78, "AWX0512244", "विक्रम कुमार",   "माता", "चन्द्र देवी",       "35",   36, "पुरुष", False),
    (79, "GKM8577751", "उर्मिला देवी",   "पति",  "बिरंजी लाल",        "38",   87, "महिला", False),
    (80, "GKM8585598", "अनिल",            "पिता", "बिरंजी लाल",        "38",   40, "पुरुष", False),
    (81, "GKM6453690", "मंजू देवी",       "पति",  "मनीष",              "42",   60, "महिला", False),
    (82, "GKM8579260", "कैलाश",           "पिता", "देवी राम",          "43",   57, "पुरुष", False),
    (83, "GKM8579278", "रूपवती",          "पति",  "कैलाश",             "43",   52, "महिला", False),
    (84, "AWX2244317", "रविन्द्र",        "पिता", "विनोद",             "43",   32, "पुरुष", False),
    (85, "AWX3443645", "राहुल शर्मा",     "पिता", "कैलाश चन्द",        "43",   32, "पुरुष", False),
    (86, "GKM8569691", "बिजेन्द्र शर्मा","पिता", "जग पाल शर्मा",      "44",   54, "पुरुष", False),
    (87, "GKM8569709", "रूपकुमारी शर्मा","पति",  "बिजेन्द्र शर्मा",  "44",   52, "महिला", False),
    (88, "GKM8574378", "कान्ता",          "पति",  "राज कुमार",         "45",   57, "महिला", False),
    (89, "GKM5932306", "कमल सिंह",        "पिता", "कृष्ण सिंह",        "45",   52, "पुरुष", False),
    (90, "GKM8076234", "कमला देवी",       "पति",  "भगवान सिंह",        "46",   47, "महिला", False),
]

data = json.loads((_JSON / "page5_results.json").read_text(encoding="utf-8"))
records = {r["sno"] + 60: r for r in data["records"]}

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

out = _GT / "compare_p5_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/30")
print(f"Field mismatches: {len(mismatches)}")
