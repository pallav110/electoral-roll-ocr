"""Would the model actually be allowed to answer? Guard pass-rate.

The Layer 0 measurement left 173 name disagreements between the rules and
IndicTrans2. Those are not all the same kind of problem, and the difference
decides the architecture:

  * conventional spelling  सिंह -> Singh, मीरा -> Meera, राजवीर -> Rajveer.
    The rules are not wrong, they are *phonetic*; English writes these names
    the way English writes them. That is a lexicon fact, and no rule derives it.

  * rule bugs               संजय -> Samjay (should be Sanjay), मांगीराम ->
    Mamgiram (should be Mangiram). The anusvara is being dropped instead of
    assimilating. That is deterministic and belongs in Layer 0.

  * model failure           चन्द्र -> "the moon", सीमा -> a 400-character
    loop, श्री -> "Mr.". Meaning-drift and degeneration. Guards must reject
    these, and a rule cannot produce them in the first place.

This measures how many disagreements survive guards that reject the third
category. That number is the real answer to "is the model worth it".
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from layer1_candidates import NAME_FIELDS, is_drift, load_eval  # noqa: E402

import app.transliterate as T  # noqa: E402

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
DIGIT_RE = re.compile(r"\d")

# A model that repeats is broken. Two repeats of the same short unit is the
# threshold: legitimate names do not repeat ("Ram Ram" is not a name).
REPEAT_RE = re.compile(r"(.{2,30}?)\1{2,}")


def guard_reasons(hindi: str, model: str) -> list[str]:
    """Every reason to REJECT the model's answer. Empty means it may be used."""
    out = model or ""
    reasons = []

    if not out.strip():
        return ["empty"]
    if DEVANAGARI_RE.search(out):
        reasons.append("devanagari residue")
    if REPEAT_RE.search(out):
        reasons.append("degenerate repetition")
    if is_drift(out):
        reasons.append("meaning drift")
    if len(out) > 3 * max(len(hindi), 4):
        reasons.append("runaway length")

    # Digits are load-bearing: a wrong digit points at the wrong house.
    if set(DIGIT_RE.findall(hindi)) - set(DIGIT_RE.findall(out)):
        reasons.append("digits lost")

    # A one-word Hindi name must not come back as a sentence fragment. This is
    # what catches "श्री" -> "Mr." and "शान्ति" -> "Mr.".
    if len(hindi.split()) == 1 and len(out.split()) > 1:
        # A compound written as two words is not necessarily wrong
        # ("रामस्वरूप" -> "Ram Swaroop"), so this is reported separately below
        # rather than treated as a hard reject.
        pass

    return reasons


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", type=int, default=40)
    args = parser.parse_args()

    rows = load_eval()
    names = [r for r in rows if r["field"] in NAME_FIELDS]

    accepted, rejected = [], []
    for row in names:
        hindi, model = row["hindi"], row["indictrans2"] or ""
        current = T.transliterate(hindi) or ""
        reasons = guard_reasons(hindi, model)
        rec = {
            "field": row["field"],
            "occ": int(row["occurrences"] or 0),
            "hindi": hindi,
            "rules": current,
            "model": model,
        }
        (rejected if reasons else accepted).append(rec)

    def row(r):
        return f"  [{r['occ']:>3}x] {r['hindi']:<20} rules={r['rules']:<18} model={r['model']}"

    print(f"name rows: {len(names)}")
    print(f"model REJECTED by guards: {len(rejected)} "
          f"({len(rejected) / max(len(names), 1) * 100:.0f}%)")
    print(f"model ACCEPTED:            {len(accepted)} "
          f"({len(accepted) / max(len(names), 1) * 100:.0f}%)")

    agree = [r for r in accepted
             if r["rules"].strip().lower() == r["model"].strip().lower()]
    differ = [r for r in accepted if r not in agree]
    print(f"  of accepted, rules and model already AGREE: {len(agree)}")
    print(f"  of accepted, genuine disagreements:        {len(differ)}")
    diff_occ = sum(r["occ"] for r in differ)
    print(f"  -> model would actually change {diff_occ} name occurrences")

    print("\n" + "=" * 72)
    print("REJECTED (evidence the model must not own names)")
    print("=" * 72)
    why = Counter()
    for r in rejected:
        for reason in guard_reasons(r["hindi"], r["model"]):
            why[reason] += 1
    for reason, count in why.most_common():
        print(f"  {reason:<26} {count}")
    for r in rejected[: args.show]:
        print(row(r) + "   <- " + ", ".join(guard_reasons(r["hindi"], r["model"])))

    print("\n" + "=" * 72)
    print("ACCEPTED and DIFFERENT from the rules (the model's real value)")
    print("=" * 72)
    for r in sorted(differ, key=lambda r: -r["occ"])[: args.show]:
        print(row(r))

    print("\n" + "=" * 72)
    print("ANUSVARA ASSIMILATION -- deterministic, belongs in Layer 0")
    print("=" * 72)
    anusvara = re.compile(r"[ंँ]")
    enriched = accepted + rejected
    sus = [r for r in enriched if anusvara.search(r["hindi"])]
    bad = [r for r in sus if r["rules"].strip().lower() != r["model"].strip().lower()]
    print(f"names containing an anusvara: {len(sus)}, of which differ: {len(bad)}")
    for r in sorted(sus, key=lambda r: -r["occ"])[:20]:
        mark = "  " if r in bad else "ok"
        print(f" {mark} [{r['occ']:>3}x] {r['hindi']:<18} rules={r['rules']:<16} model={r['model']}")


if __name__ == "__main__":
    main()