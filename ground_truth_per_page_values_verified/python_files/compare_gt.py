#!/usr/bin/env python3
"""Compare page3_results.json against ground truth and write diff to compare_results.txt"""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (1,  "AWX4353694", "दुर्गेश", "अन्य", "कुंवरपाल", "",         38, "महिला", True),
    (2,  "AWX1551100", "सचिन मेहता", "पिता", "राजकुमार", "00",   34, "पुरुष", False),
    (3,  "AWX1551092", "सनी मेहता", "पिता", "राजकुमार", "00",    33, "पुरुष", False),
    (4,  "GKM7543309", "ममता देवी", "पति", "सुनील कुमार", "1",   50, "महिला", False),
    (5,  "AWX5012331", "कल्पना", "पति", "मनोज कुमार शर्मा", "1/1044", 47, "महिला", False),
    (6,  "GKM8922650", "काशी राम", "पिता", "सुमेर चन्द", "3",    60, "पुरुष", False),
    (7,  "GKM6453740", "धनपति देवी", "पति", "काशीराम", "3",      57, "महिला", False),
    (8,  "GKM8569717", "रेखा", "पति", "महावीर", "4",             52, "महिला", False),
    (9,  "AWX1057132", "श्री मति देवी", "पति", "किशनपाल सिंह", "5", 52, "महिला", False),
    (10, "GKM5931852", "ब्रह्मपाल", "पिता", "रणधीर सिंह", "7",   57, "पुरुष", False),
    (11, "GKM5931860", "राजेश देवी", "पति", "ब्रह्म पाल", "7",   52, "महिला", False),
    (12, "AWX2456275", "भीकम सिंह", "पिता", "किशन", "7 बी",     54, "पुरुष", False),
    (13, "AWX2455913", "रोहित", "पिता", "भीकम", "7 बी",          33, "पुरुष", False),
    (14, "AWX2455921", "बबीता", "पिता", "भीकम", "7 बी",          33, "महिला", False),
    (15, "GKM8576993", "राजपाल", "पिता", "लक्ष्मन सिंह", "8",   62, "पुरुष", False),
    (16, "GKM5931878", "मुनेश देवी", "पति", "राजपाल", "8",       57, "महिला", False),
    (17, "AWX3157146", "पूनम गर्ग", "पति", "राजकुमार गर्ग", "8", 49, "महिला", False),
    (18, "AWX3157575", "कृपाल", "पिता", "लखपत सिंह", "8/24",    38, "पुरुष", False),
    (19, "AWX3157104", "शारदा देवी", "पति", "कृपाल", "8/24",     36, "महिला", False),
    (20, "AWX3157385", "अमित", "पिता", "राजबीर", "8/81",         39, "पुरुष", False),
    (21, "AWX2532729", "मोन्टी शर्मा", "पिता", "योगेश शर्मा", "8/444", 32, "पुरुष", False),
    (22, "AWX2245264", "सन्तोष", "पति", "हीरा लाल", "8/468",      36, "महिला", False),
    (23, "AWX5011077", "विजय बालियान", "पिता", "मगेन्द्र सिंह", "8/483", 23, "पुरुष", False),
    (24, "AWX3707148", "घनश्याम पाण्डेय", "पिता", "राजदेव पाण्डेय", "8/532", 52, "पुरुष", False),
    (25, "AWX4972535", "आकाश", "पिता", "कैलाश गिरी", "8/588",    24, "पुरुष", False),
    (26, "AWX3707189", "अशोक", "पिता", "जगदीश", "8/829",         37, "पुरुष", False),
    (27, "AWX3157633", "रीतू", "पति", "नरेन्द्र", "8इ-526",        40, "महिला", False),
    (28, "AWX2531556", "सुनीता देवी", "पति", "महेश कुमार शर्मा", "9", 53, "महिला", False),
    (29, "AWX1551175", "रणबीर सिंह".strip(), "पिता", "गंगाशरण", "09",    49, "पुरुष", False),
    (30, "AWX1551183", "कुसुम", "पति", "रणबीर सिंड", "09",       46, "महिला", False),
]

data = json.loads((_JSON / "page3_results.json").read_text(encoding="utf-8"))
records = {r["sno"]: r for r in data["records"]}

lines = []
mismatches = []

def ocr_fullname(r, rel):
    """Reconstruct full relation name from split fields."""
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
        mismatches.append((sno, "MISSING", "", ""))
        continue

    # EPIC
    ocr_epic = r.get("id_card_no", "")
    if ocr_epic != epic:
        row_issues.append(f"EPIC: got={ocr_epic!r} want={epic!r}")

    # For deleted cards only check is_deleted — text fields are covered by watermark
    if deleted:
        ocr_del = r.get("is_deleted", False)
        if not ocr_del:
            row_issues.append(f"is_deleted: got={ocr_del!r} want=True")
        if row_issues:
            lines.append(f"[{sno:2d}] {epic} MISMATCH:")
            for issue in row_issues:
                lines.append(f"      {issue}")
            mismatches.extend([(sno, i) for i in row_issues])
        else:
            lines.append(f"[{sno:2d}] {epic} OK")
        continue

    # Name
    ocr_name = ocr_fullvoter(r)
    if ocr_name != name:
        row_issues.append(f"name: got={ocr_name!r} want={name!r}")

    # Relation type
    ocr_rel = r.get("relation_name", "")
    if ocr_rel != rel and not (sno == 1 and deleted):  # skip deleted card relation
        row_issues.append(f"relation_type: got={ocr_rel!r} want={rel!r}")

    # Relation name
    ocr_rname = ocr_fullname(r, rel)
    if ocr_rname != rel_name and not (sno == 1 and deleted):
        row_issues.append(f"relation_name: got={ocr_rname!r} want={rel_name!r}")

    # House
    ocr_house = r.get("house_no", "")
    if ocr_house != house and not (sno == 1 and deleted):
        row_issues.append(f"house_no: got={ocr_house!r} want={house!r}")

    # Age
    ocr_age = str(r.get("age", ""))
    if ocr_age != str(age):
        row_issues.append(f"age: got={ocr_age!r} want={str(age)!r}")

    # Gender
    ocr_gender = r.get("gender", "")
    if ocr_gender != gender:
        row_issues.append(f"gender: got={ocr_gender!r} want={gender!r}")

    # Deleted
    ocr_del = r.get("is_deleted", False)
    if ocr_del != deleted:
        row_issues.append(f"is_deleted: got={ocr_del!r} want={deleted!r}")

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

out = _GT / "compare_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/30")
print(f"Field mismatches: {len(mismatches)}")
