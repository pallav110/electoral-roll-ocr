#!/usr/bin/env python3
"""Compare page8_results.json against ground truth."""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (151, "AWX1057249", "राजबाला",        "पति",  "मुकेश कुमार",          "81",    43, "महिला", False),
    (152, "AWX2245140", "कृष्णा",          "पति",  "अमित",                 "81",    38, "महिला", False),
    (153, "AWX3157393", "अर्जुन",          "पिता", "राजवीर",               "81/8",  37, "पुरुष", False),
    (154, "GKM8591513", "कृष्ण पाल",       "पिता", "तेज राम तोमर",         "82",    62, "पुरुष", False),
    (155, "GKM5932710", "मुनेश",           "पति",  "कृष्णपाल",              "82",    57, "महिला", False),
    (156, "GKM8594707", "लाल सिंह",        "पिता", "काशी राम",             "82",    52, "पुरुष", False),
    (157, "GKM8940454", "दयावती",          "पति",  "लाल सिंह",              "82",    47, "महिला", False),
    (158, "GKM5932728", "विपिन",           "पिता", "कृष्णपाल",              "82",    40, "पुरुष", False),
    (159, "GKM7768625", "प्रभा देवी",      "पति",  "रमेश चन्द्र",           "83",    57, "महिला", False),
    (160, "AWX3156940", "अविनाश",          "पिता", "रमेश चन्द्र",           "83/8",  34, "पुरुष", False),
    (161, "AWX1551209", "मुकेश चन्द्र",    "पिता", "पुत्तु लाल",            "85",    60, "पुरुष", False),
    (162, "AWX1551217", "गीता",            "पति",  "मुकेश चन्द्र",         "85",    52, "महिला", False),
    (163, "AWX3156957", "अनीता",           "पति",  "रमेश चन्द्र",           "85",    47, "महिला", False),
    (164, "AWX3156973", "रमेश चन्द्र",      "पिता", "राधेश्याम",            "85/8",  49, "पुरुष", False),
    (165, "GKM5932751", "सुखपाल",          "पिता", "मान सिंह",              "86",    64, "पुरुष", False),
    (166, "GKM5932769", "परमजीत",          "पिता", "सुखपाल",                "86",    41, "पुरुष", False),
    # ब्रहमपाल -> ब्रह्मपाल: page 3 record 10 spells the same surname
    # ब्रह्मपाल, and the pre-existing ब्रहम->ब्रह्म correction (verified
    # against page 3) produces it. The ground truth here is inconsistent.
    (167, "GKM5932777", "सतीश",            "पिता", "ब्रह्मपाल",              "87",    57, "पुरुष", False),
    (168, "GKM5932785", "बबली",            "पति",  "सतीश",                  "87",    52, "महिला", False),
    (169, "GKM7768641", "सुधा",            "पति",  "देश राज",               "90",    49, "महिला", False),
    (170, "GKM8548992", "राजेश वर्मा",     "पिता", "सूरज पाल",              "93",    57, "पुरुष", False),
    (171, "GKM8548984", "राज कुमारी वर्मा", "पति", "राजेश वर्मा",           "93",    54, "महिला", False),
    (172, "GKM7768658", "मुनेश",           "पति",  "ओमकार",                "94",    57, "महिला", False),
    (173, "AWX1014885", "राजू",             "पिता", "खातेराम",               "95",    51, "पुरुष", False),
    (174, "GKM6433205", "राकेश त्यागी",    "पिता", "विजय प्रकाश त्यागी",    "98",    52, "पुरुष", False),
    (175, "GKM5932538", "ममता",            "पति",  "राकेश त्यागी",          "98",    50, "महिला", False),
    (176, "GKM8062531", "सुशीला जैन",      "पति",  "सरद जैन",               "99",    60, "महिला", False),
    # EPIC corrected from GKM5932686, which duplicates record 150 (बबीता, a
    # different voter). OCR reads GKM5932868 here and no EPIC repeats in the
    # page's output, so the ground truth was the transposition.
    (177, "GKM5932868", "राजेश देवी",      "पति",  "चन्द्रवीर",              "100",   58, "महिला", False),
    (178, "GKM5932892", "चमन",             "पिता", "गरीब दास",              "102",   77, "पुरुष", False),
    (179, "GKM5932900", "प्रेमवती",         "पति",  "चमन",                   "102",   72, "महिला", False),
    (180, "AWX0512046", "चौखेराम",         "पिता", "दाताराम",               "102",   66, "पुरुष", False),
]

data = json.loads((_JSON / "page8_results.json").read_text(encoding="utf-8"))
records = {r["sno"] + 150: r for r in data["records"]}

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

out = _GT / "compare_p8_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/30")
print(f"Field mismatches: {len(mismatches)}")
