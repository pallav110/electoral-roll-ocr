"""Where does the rule-based path still disagree with IndicTrans2, on names?

The Layer 0 scheme fix (ITRANS) changed the rule-based column of
indictrans2_eval.csv, which was measured before it. This regenerates that
column and re-diffs against the stored IndicTrans2 output.

The disagreements are the candidate set for Layer 1 (an exact map): words the
rules get wrong on *every* occurrence. Disagreements where the rules are right
and the model drifted (आकाश -> "the sky") are not candidates -- those are the
model's failures, and a map is the right fix precisely because a rule cannot
make them.

Usage:
    python tests/test_scripts/layer1_candidates.py
    python tests/test_scripts/layer1_candidates.py --field voter_first_name
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import app.transliterate as T  # noqa: E402

EVAL_CSV = REPO_ROOT / "indictrans2_eval.csv"

NAME_FIELDS = {
    "voter_first_name",
    "voter_middle_name",
    "voter_sur_name",
    "voter_father_first_name",
    "voter_father_middle_name",
    "voter_father_last_name",
    "voter_husband_first_name",
    "voter_husband_middle_name",
    "voter_husband_last_name",
    "voter_mother_first_name",
    "voter_other_first_name",
}

# IndicTrans2 is a translation model. These are correct translations and wrong
# answers for a name field -- the model answered a different question. Not
# candidates for a map; they are evidence the model must not own names alone.
SEMANTIC_DRIFT = {
    "the sky", "joy", "imagination", "relax", "peace", "wealth", "the moon",
    "sun", "lord", "goddess", "flower", "the earth", "student", "teacher",
    "king", "queen", "brave", "new", "the best",
}


def is_drift(english: str) -> bool:
    low = (english or "").strip().lower().rstrip(".")
    if not low:
        return False
    return low in SEMANTIC_DRIFT or low.startswith("the ")


def load_eval():
    with open(EVAL_CSV, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", default=None, help="restrict to one field")
    parser.add_argument("--min-occurrences", type=int, default=1,
                        help="only words seen at least this many times")
    args = parser.parse_args()

    rows = load_eval()
    out = []
    for row in rows:
        hindi = row["hindi"]
        # House numbers go through house_en(); everything else through the
        # general path. Using the wrong one would compare against a column
        # that was never produced for that field.
        if row["field"] == "house_no":
            current = T.house_en(hindi)
        else:
            current = T.transliterate(hindi)
        out.append({
            "field": row["field"],
            "occurrences": int(row["occurrences"] or 0),
            "hindi": hindi,
            "stale_rule_based": row["rule_based"],
            "current": current or "",
            "model": row["indictrans2"] or "",
        })

    names = [r for r in out if r["field"] in NAME_FIELDS]
    print(f"total rows {len(out)}  name rows {len(names)}")

    drift = [r for r in names if is_drift(r["model"])]
    print(f"model meaning-drift on names: {len(drift)} "
          f"({len(drift) / max(len(names), 1) * 100:.0f}% of name rows)\n")

    # Only rows where both engines produced something comparable.
    comparable = [
        r for r in names
        if r["current"] and r["model"] and not is_drift(r["model"])
    ]
    differ = [
        r for r in comparable
        if r["current"].strip().lower() != r["model"].strip().lower()
        and r["occurrences"] >= args.min_occurrences
    ]
    if args.field:
        differ = [r for r in differ if r["field"] == args.field]

    total_occ = sum(r["occurrences"] for r in comparable)
    diff_occ = sum(r["occurrences"] for r in differ)
    print(f"comparable name rows: {len(comparable)}")
    print(f"disagreements: {len(differ)}  "
          f"({diff_occ}/{total_occ} occurrences, "
          f"{diff_occ / max(total_occ, 1) * 100:.1f}%)\n")

    print("=" * 72)
    print("CANDIDATES for a Layer 1 exact map (rules and model disagree)")
    print("=" * 72)
    for r in sorted(differ, key=lambda r: (-r["occurrences"], r["hindi"])):
        print(f"  [{r['occurrences']:>3}x] {r['hindi']:<20} "
              f"rules={r['current']:<18} model={r['model']}")

    print("\n" + "=" * 72)
    print("MODEL MEANING-DRIFT on names (evidence against model-owned names)")
    print("=" * 72)
    for r in sorted(drift, key=lambda r: -r["occurrences"])[:20]:
        print(f"  [{r['occurrences']:>3}x] {r['hindi']:<20} "
              f"rules={r['current']:<18} model={r['model']}")

    print(f"\nfield mix of disagreements: "
          f"{dict(Counter(r['field'] for r in differ))}")


if __name__ == "__main__":
    main()