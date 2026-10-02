"""Generate the Layer 1 proper-noun spelling lexicon.

Layer 1 answers the one question no rule can: how does *English* write this
particular name. The rules produce a phonetic transliteration (पाल -> Pala);
English writes it Pal. That is a lexicon fact, not a derivable one, and it is
the only thing a map is good for.

Automatability is the point of this generator, so nothing here asks a human to
approve a spelling. Three filters decide every entry, and each has to be
provable rather than a matter of taste:

  1. The model output must survive every guard in model_guards. Drift,
     degenerate repetition, detached diacritics and lost digits are all
     rejections -- an entry learned from those would put garbage in the map.
  2. The disagreement must be a *spelling* difference, not a meaning one.
     Guard 1 mostly buys this, but "Pal" vs "Pala" and "Ram" vs "Rama" are
     genuinely ambiguous without it.
  3. The model output must look like a romanisation of the same name, not a
     different word. A cheap proxy: it must start with the same initial letter
     as the rule-based output, and differ from it only by letter length,
     doubled letters, or vowel/ee length.

Filter 3 is what lets this run unattended. It rejects "Victory" for विजय and
"Hero" for वीर without anyone reading them.

Usage:
    python tests/test_scripts/build_layer1_map.py
    python tests/test_scripts/build_layer1_map.py --min-occurrences 1
    python tests/test_scripts/build_layer1_map.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from layer1_candidates import NAME_FIELDS, load_eval  # noqa: E402
from model_guards import guard_reasons, normalise  # noqa: E402

import app.transliterate as T  # noqa: E402

OUT_JSON = REPO_ROOT / "app" / "name_lexicon.json"


def rules_only(text: str) -> str:
    """The Layer 0 answer, with Layer 1 deliberately bypassed.

    The generator has to compare the model against what the *rules* produce.
    Once an entry is in name_lexicon.json, transliterate() returns the model's
    spelling instead, so comparing against it would show zero disagreement and
    the map could never be regenerated -- it would silently empty itself.
    """
    saved = T._NAME_LEXICON_CACHE
    T._NAME_LEXICON_CACHE = {}
    try:
        return T.transliterate(text) or ""
    finally:
        T._NAME_LEXICON_CACHE = saved


def squash(value: str) -> str:
    """Collapse the two systematic, non-error spelling differences.

    Doubled letters and the ee/ii vowel are conventions, not mistakes, so two
    names that differ only in those are the same name written two ways.

    ORDER MATTERS, and getting it wrong is silent. Collapsing doubled letters
    first consumes the "ee" in "Sandeep" as a repeated character, so the vowel
    fold never sees it and Sanjiv/Sanjeev fail to meet -- which is exactly the
    pair the comparison exists to accept. Fold the vowel, then the doubling.
    """
    value = re.sub(r"[^a-z]", "", value.lower())
    value = value.replace("ee", "i").replace("ii", "i")
    return re.sub(r"(.)\1+", r"\1", value)


def looks_like_a_spelling(rules: str, model: str) -> bool:
    """Is `model` a plausible English spelling of the same name as `rules`?

    Conservative by design: a false negative costs one name that keeps its
    phonetic transliteration, a false positive writes a wrong name into the
    map and into every future database row.

    The test is a small edit distance, not equality. Almost every real case is
    a dropped or doubled vowel -- Pala/Pal, Ram/Rama, Sanjiv/Sanjeev,
    Punam/Poonam, Verma/Varma -- so requiring the two to match exactly rejects
    every entry worth having, which is what the first version of this function
    did.
    """
    a, b = normalise(rules), normalise(model)
    if not a or not b:
        return False
    if a == b:
        return False

    # Different number of words entirely is a translation, not a spelling.
    if len(a.split()) != len(b.split()):
        return False

    # "Pal" for "Pala" is a spelling difference; "Prince" for "Rajakumar" is
    # not. Same initial letter is the cheapest signal that survives both.
    if a[0].lower() != b[0].lower():
        return False

    sa, sb = squash(a), squash(b)

    # Never let the map lengthen a name. The rules already drop the inherent
    # schwa wherever English drops it (पाल -> "Pala", and English writes
    # "Pal"), so a model output *longer* than the rules means it invented a
    # vowel: शिव -> "Shiva", महावीर -> "Mahavira". "Shiv" is the correct
    # English spelling. Compared on the raw strings, because squashing hides
    # exactly this difference.
    if len(b) > len(a):
        return False

    if sa == sb:
        return True

    # One or two edits, relative to the shorter string, covers a dropped
    # vowel plus the occasional consonant swap (Datta/Dutt, Pavar/Pawar).
    return _levenshtein(sa, sb) <= max(2, len(sa) // 4)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (ca != cb),  # substitution
            ))
        previous = current
    return previous[-1]


def collect(min_occurrences: int, field_filter: str | None):
    merged = {}
    for row in load_eval():
        hindi = row["hindi"]
        occ = int(row["occurrences"] or 0)
        if hindi in merged:
            merged[hindi]["occurrences"] += occ
            merged[hindi]["fields"].add(row["field"])
            continue
        merged[hindi] = {
            "fields": {row["field"]},
            "occurrences": occ,
            "hindi": hindi,
            "rules": rules_only(hindi),
            "model": (row["indictrans2"] or "").strip(),
        }

    entries, rejected = [], []
    for rec in merged.values():
        if rec["fields"] & NAME_FIELDS == set():
            continue
        if rec["occurrences"] < min_occurrences:
            continue

        reasons = guard_reasons(rec["hindi"], rec["model"])
        if reasons:
            rejected.append({**rec, "reasons": reasons})
            continue
        if not looks_like_a_spelling(rec["rules"], rec["model"]):
            rejected.append({**rec, "reasons": ["not a spelling difference"]})
            continue

        entries.append(rec)

    entries.sort(key=lambda r: (-r["occurrences"], r["hindi"]))
    return entries, rejected


def main():
    # The report is half Devanagari, and stdout follows the console code page.
    # On a stock Windows console that is cp1252, which cannot encode these
    # characters at all -- the run would compute every entry correctly and then
    # die printing them. Forcing UTF-8 here makes the generator behave the same
    # in any terminal rather than depending on ambient console state.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--min-occurrences", type=int, default=2)
    parser.add_argument("--field", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    entries, rejected = collect(args.min_occurrences, args.field)

    print(f"accepted {len(entries)} entries, "
          f"rejected {len(rejected)}\n")
    for rec in entries:
        print(f"  [{rec['occurrences']:>3}x] {rec['hindi']:<18} "
              f"{rec['rules']:<18} -> {rec['model']}")

    print(f"\ncovers {sum(r['occurrences'] for r in entries)} name occurrences")

    if args.dry_run:
        print("\ndry run, nothing written")
        return

    payload = {
        "_comment": (
            "Layer 1 proper-noun spelling lexicon. Hindi -> the spelling English "
            "uses, where that differs from the phonetic transliteration. "
            "Generated by tests/test_scripts/build_layer1_map.py from the "
            "measured IndicTrans2 disagreements; entries are only accepted "
            "when the model output passes every guard and differs from the "
            "rules only by spelling. See DOCS/TRANSLITERATION_ENGINE_ROUTING.md."
        ),
        "entries": {rec["hindi"]: rec["model"] for rec in entries},
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()