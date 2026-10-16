import sys, json
sys.stdout = open("/data/e2e_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import ocr_pdf_api as A

for label, path in (("S24-300 WORKS", "/data/roll_s24_300.pdf"),
                    ("S28-40 Lansdown", "/data/roll107.pdf")):
    print("=" * 74); print(label); print("=" * 74)
    with open(path, "rb") as fh:
        blob = fh.read()
    res = A._extract_pdf_ocr_unlocked(blob, path.split("/")[-1], 3, 4, False, True)
    recs = res.get("records", []) if isinstance(res, dict) else []
    print(f"records on pages 3-4: {len(recs)}")
    named = [r for r in recs if (r.get("voter_first_name") or r.get("voter_sur_name"))]
    withrel = [r for r in recs if any(r.get(k) for k in (
        "voter_father_name", "voter_husband_name", "voter_mother_name", "voter_other_name"))]
    print(f"  with a name:      {len(named)}/{len(recs)}")
    print(f"  with a relation:  {len(withrel)}/{len(recs)}")
    for r in recs[:8]:
        print("   ", {k: v for k, v in r.items() if v and k in (
            "voter_sr_no", "epic_number", "age", "gender", "house_number",
            "voter_first_name", "voter_sur_name", "relation_name",
            "voter_father_name", "voter_husband_name")})
    print()