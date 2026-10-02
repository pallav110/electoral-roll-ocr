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

# One guard, imported rather than reimplemented. Two drift lists in two scripts
# is two lists that will disagree, and the disagreement shows up as a wrong
# name in an English column.
from model_guards import guard_reasons, has_orphan_marks, is_drift, normalise as _normalise  # noqa: E402

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

# Drift detection lives in model_guards so the two evaluation scripts cannot
# disagree about what counts as drift.


def load_eval():
    with open(EVAL_CSV, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", default=None, help="restrict to one field")
    parser.add_argument("--min-occurrences", type=int, default=1,
                        help="only words seen at least this many times")
    args = parser.parse_args()

    # A name appears once per field it was found in (a first name and a
    # father's first name, say), so the same string shows up several times.
    # Sum the occurrences and keep one row per string, or पाल shows up twice
    # and the map gets a duplicate entry.
    merged = {}
    for row in load_eval():
        hindi = row["hindi"]
        occ = int(row["occurrences"] or 0)
        if hindi in merged:
            merged[hindi]["occurrences"] += occ
            merged[hindi]["fields"].add(row["field"])
            continue
        merged[hindi] = {
            "field": row["field"],
            "fields": {row["field"]},
            "occurrences": occ,
            "hindi": hindi,
            # House numbers go through house_en(); everything else through the
            # general path. Using the wrong one would compare against a column
            # that was never produced for that field.
            "current": T.house_en(hindi) if row["field"] == "house_no"
                       else (T.transliterate(hindi) or ""),
            "model": row["indictrans2"] or "",
        }

    out = list(merged.values())

    # Run the full guard suite and record *why* each rejection happened,
    # rather than blanking the model column and then reporting 0% drift --
    # which is what an earlier version did, and it hid every rejection.
    for row in out:
        row["reasons"] = guard_reasons(row["hindi"], row["model"])
        row["usable"] = not row["reasons"]

    names = [r for r in out if r["field"] in NAME_FIELDS]
    print(f"total rows {len(out)}  name rows {len(names)}")

    drift = [r for r in names if is_drift(r["model"])]
    rejected = [r for r in names if not r["usable"]]
    print(f"model rejected by guards on names: {len(rejected)} "
          f"({len(rejected) / max(len(names), 1) * 100:.0f}%)")
    print(f"model meaning-drift on names: {len(drift)} "
          f"({len(drift) / max(len(names), 1) * 100:.0f}% of name rows)\n")

    # Only rows where the model survived the guards and both engines produced
    # something comparable.
    comparable = [r for r in names if r["current"] and r["model"] and r["usable"]]
    differ = [
        r for r in comparable
        if _normalise(r["current"]) != _normalise(r["model"])
        and r["occurrences"] >= args.min_occurrences
    ]
    if args.field:
        differ = [r for r in differ if args.field in r["fields"]]

    total_occ = sum(r["occurrences"] for r in comparable)
    diff_occ = sum(r["occurrences"] for r in differ)
    print(f"comparable name strings: {len(comparable)}")
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
    print("MODEL REJECTED by guards (never candidates for the map)")
    print("=" * 72)
    for r in sorted(rejected, key=lambda r: -r["occurrences"])[:20]:
        print(f"  [{r['occurrences']:>3}x] {r['hindi']:<20} "
              f"rules={r['current']:<18} model={r['model'][:40]!r}")
        print(f"        rejected: {', '.join(r['reasons'])}")

    print(f"\nfield mix of disagreements: "
          f"{dict(Counter(r['field'] for r in differ))}")


if __name__ == "__main__":
    main()