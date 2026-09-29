#!/usr/bin/env python3
"""Compare page21_results.json against ground truth (tricky address page)."""
import json
from pathlib import Path

# Scripts live one level below the folder that holds the results, so paths
# are anchored to the parent rather than to the current working directory.
_ROOT = Path(__file__).resolve().parent.parent
_JSON = _ROOT / "json_files"
_GT = _ROOT / "gt_text_files"

GROUND_TRUTH = [
    (525, "AWX6395115", "प्रमोद",         "अन्य", "सुनीता",              "449/9",                  47, "पुरुष", False),
    (526, "AWX6401764", "जुबैर अली",      "पिता", "जुल्फिकार अली",        "प्लॉट नं 279 ख नं 79",  28, "पुरुष", False),
    (527, "AWX6422448", "अरवाज",          "पिता", "मोहम्मद यासीन",       "बी-89",                  28, "पुरुष", False),
    (528, "AWX6427629", "सीमा देवी",      "पति",  "अमलेशवर कुमार सिंह",  "इ-1/75",                46, "महिला", False),
    (529, "AWX6430284", "नीलोफर",         "पिता", "अय्यूब अली",          "एचएनओ 146",             25, "महिला", False),
    (530, "AWX6430722", "प्रियंका जोशी",  "पति",  "धर्मेंद्र",             "09/121",                 24, "महिला", False),
    (531, "YHL2423225", "मोहम्मद तकी",    "पिता", "अफजलाल हैदर",         "पी. नं-बी 190, ख नं-701", 29, "पुरुष", False),
]

data = json.loads((_JSON / "page21_results.json").read_text(encoding="utf-8"))
# Page 21 is the 21st voter page, so its records start at 21*30 - 30 + 1 = 601
# only if every prior page held 30 cards. The GT above starts at 525, so the
# offset is derived from the smallest GT serial rather than assumed.
offset = min(g[0] for g in GROUND_TRUTH) - min(r["sno"] for r in data["records"])
records = {r["sno"] + offset: r for r in data["records"]}

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


print(f"[p21] offset applied: {offset} (sno {min(r['sno'] for r in data['records'])}..{max(r['sno'] for r in data['records'])})")
print(f"[p21] roll_metadata: {json.dumps(data.get('roll_metadata', {}), ensure_ascii=False)}")

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
lines.append(f"Total mismatched records: {len(set(m[0] for m in mismatches))}/{len(GROUND_TRUTH)}")
lines.append(f"Total field mismatches: {len(mismatches)}")

out = _GT / "compare_p21_results.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Written to {out}")
print(f"Mismatched records: {len(set(m[0] for m in mismatches))}/{len(GROUND_TRUTH)}")
print(f"Field mismatches: {len(mismatches)}")
