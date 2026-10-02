"""Corpus sweep: what does transliterate() still emit that should never appear?

Runs the real ground-truth strings through transliterate() and reports
residue that survives into an English column: library digraph markers, stray
escapes, Devanagari that failed to convert, and whitespace damage.

This is the regression detector for romanisation-layer changes. A scheme switch
or a new cleanup rule can silently reintroduce any of these -- the unit tests
pin known cases, this pins the whole corpus.

Usage:
    python tests/test_scripts/sweep_corpus.py
    python tests/test_scripts/sweep_corpus.py --verbose   # list every row
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_indictrans2_vs_rulebased import load_corpus  # noqa: E402

import app.transliterate as T  # noqa: E402

# '~' and '`' are the library's marker for the rare nasals ङ/ञ.
# '\' and "'" are what ITRANS emits for the udatta/anudatta stress signs.
MARKER_RE = re.compile("[~`\\\\'\"]")
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def sweep():
    """Return {check_name: [(hindi, english), ...]} over the whole corpus."""
    unique, _field_of, _counts, _ = load_corpus(None)
    out = [T.transliterate(t) for t in unique]
    rows = list(zip(unique, out))

    def where(pred):
        return [(t, o) for t, o in rows if pred(o or "")]

    return {
        "library markers / stray escapes": where(lambda o: MARKER_RE.search(o)),
        "Devanagari leaking into English": where(lambda o: DEVANAGARI_RE.search(o)),
        "whitespace damage": where(lambda o: o != o.strip() or "  " in o),
        "returned empty": where(lambda o: not o),
    }, unique


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    checks, unique = sweep()
    print(f"corpus: {len(unique)} unique strings\n")
    failed = 0
    for label, rows in checks.items():
        failed += len(rows)
        print(f"{label}: {len(rows)}")
        for t, o in rows[:40 if args.verbose else 12]:
            print(f"    {t!r:<34} -> {o!r}")
        print()
    print("CLEAN" if not failed else f"{failed} issue(s) to fix")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())