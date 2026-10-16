import sys, re
sys.stdout = open("/data/ab_full_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import ocr_pdf_api as A

ORIGINAL_SCORE = lambda lines: (
    len(re.findall(r"[ऀ-ॿ]", " ".join(lines)))
    + sum(w for lab, w in A.HINDI_FIELD_LABELS if lab in " ".join(lines))
)

FILES = [("S24-300", "/data/roll_s24_300.pdf"), ("S28-40 Lansdown", "/data/roll107.pdf")]
NAME_F = ("voter_first_name", "voter_middle_name", "voter_sur_name")
REL_F = ("voter_father_name", "voter_husband_name", "voter_mother_name", "voter_other_name")

def run(label, path, scorer):
    saved = A._hindi_line_score
    A._hindi_line_score = scorer
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
        res = A._extract_pdf_ocr_unlocked(blob, path.split("/")[-1], 3, 3, False, True)
        recs = res.get("records", [])
        n = sum(1 for r in recs if any(r.get(k) for k in NAME_F))
        r_ = sum(1 for r in recs if any(r.get(k) for k in REL_F))
        rt = sum(1 for r in recs if r.get("relation_name"))
        print(f"  {label:22} records={len(recs):3}  with_name={n:3}  "
              f"relation_type={rt:3}  with_relation_NAME={r_:3}")
        return recs
    finally:
        A._hindi_line_score = saved

for label, path in FILES:
    print("=" * 74); print(label); print("=" * 74)
    old = run("OLD (deva count)", path, ORIGINAL_SCORE)
    new = run("NEW (label score)", path, A._hindi_line_score)
    print("\n  sample records under NEW:")
    for r in new[:5]:
        print("   ", {k: v for k, v in r.items() if v and k in (
            "voter_sr_no", "voter_first_name", "voter_sur_name", "relation_name",
            *REL_F)})
    print("\n  same cards under OLD:")
    for r in old[:5]:
        print("   ", {k: v for k, v in r.items() if v and k in (
            "voter_sr_no", "voter_first_name", "voter_sur_name", "relation_name",
            *REL_F)})
    print()