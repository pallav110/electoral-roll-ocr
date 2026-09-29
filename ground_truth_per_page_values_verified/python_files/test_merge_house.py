#!/usr/bin/env python3
"""Unit tests for _merge_text_with_digits against the observed page-21 cases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import ocr_pdf_api as m  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")


merge = m._merge_text_with_digits

print("=== card 529: leading 1 recovered (GT confirmed this value) ===")
check("529", merge("एचएनओ 46", "146", ["Hch", "146"]), "एचएनओ 146")

print()
print("=== card 531: leading 1 recovered, trailing danda promoted ===")
# Traced reads: Tesseract 'प्रा: पी. नं-बी 90, ख नं-70॥', Paddle '.-190.g-701'.
# The pipeline's parser value already carries the recovered 190, so the merge
# is handed that plus the un-promoted danda.
check("531", merge("पी. नं-बी 190, ख नं-70॥", "-190-70", [".-190.g-701"]),
      "पी. नं-बी 190, ख नं-701")
# And from the un-recovered read, in one pass:
check("531 from raw read",
      merge("पी. नं-बी 90, ख नं-70॥", "-190-70", [".-190.g-701"]),
      "पी. नं-बी 190, ख नं-701")

print()
print("=== card 528: must NOT be invented ===")
# Tesseract 'इ-/75', Paddle ':3-1/75'. The '1' has no digit run of its own
# before the '/', so there is no alignment to make and the merge declines.
check("528 declines", merge("इ-/75", "1/75", [":3-1/75"]), "")

print()
print("=== card 528: leading 1 restored from two agreeing engines ===")
# The wider house band reads '] /75', and ']' is how this model renders a
# printed '1', so it promotes to parts ['1', '75'] with the slash between.
# Paddle reads the same '1/75'. Both engines agree on a digit the parser
# value lost, so it goes back.
CORR = ["1", "75", "5"]
check("528 leading 1 restored",
      merge("इ-/75", "1/75", [":3-1/75"], CORR), "इ-1/75")
# A one-digit Paddle read of a two-digit value must not win the window. The
# value crop of 'इ-1/75' reads '5', which used to be accepted as '75' with a
# dropped digit and rewrote the value to 'इ-/5'.
check("shorter Paddle run declines",
      merge("इ-/75", "5", ["5"], CORR), "")
# The digits have to appear in the corroborating read in the order Paddle
# read them; the slash is what puts them in that order.
check("out-of-order parts decline",
      merge("इ-/75", "1/75", [":3-1/75"], ["75", "1"]), "")
# Every part has to be corroborated, not just the extra one.
check("uncorroborated part declines",
      merge("इ-/75", "1/75", [":3-1/75"], ["75"]), "")
# Nothing extra to restore when the value already holds the parts.
check("nothing extra declines",
      merge("प्लॉट नं 75", "75", ["75"], CORR), "")
# The serial-bleed blob must not be treated as a clean number.
check("serial bleed declines",
      merge("इ 17500", "17500", [":3-1/750 0"], CORR), "")
# No slash in Paddle's value means nothing to place a leading digit against.
check("no slash declines",
      merge("इ-/75", "75", ["75"], CORR), "")

print()
print("=== a digit lost from the MIDDLE of a run also aligns ===")
# Card 531 as the pipeline actually reaches it: Tesseract read '71' where
# Paddle read '701' — the leading 1 of the second number was dropped, and a
# prefix/suffix test alone would reject it.
check("531 live parser value",
      merge("पी. नं-बी 90, ख नं-7!", "-190-70", [".-190.g-701"]),
      "पी. नं-बी 190, ख नं-701")
check("middle loss aligns", merge("एचएनओ 07", "107", ["107"]), "एचएनओ 107")

print()
print("=== negative cases: pure-numeric houses never reach the merge ===")
for t, p, pt in [("449/9", "449/9", ["449/9"]), ("09/121", "09/121", ["09/121"]),
                 ("", "146", ["146"])]:
    check(f"no-op {t!r}", merge(t, p, pt), "")

print()
print("=== alignment guards ===")
check("run-count mismatch declines",
      merge("पी. नं-बी 90, ख नं-70", "90", ["90"]), "")
check("paddle shorter declines", merge("एचएनओ 146", "46", ["46"]), "")
check("no paddle digits declines", merge("एचएनओ", "abc", ["abc"]), "")
check("non-overlapping declines", merge("इ-/75", "46", ["46"]), "")
# Two differing positions is a different number, not a misread digit.
check("two-digit difference declines", merge("एचएनओ 146", "175", ["175"]), "")
# Order matters: '75' is not reachable inside '46' however it is scanned.
check("out-of-order declines", merge("एचएनओ 75", "46", ["46"]), "")
# A gap of two dropped digits is a different number, not a mangled read.
# A bare subsequence test would wrongly let this through.
check("two-digit gap declines", merge("एचएनओ 46", "1469", ["1469"]), "")
# Identical runs align. One run can be a recovery while the next was read
# correctly, and a strict "differs by one" test would veto the recovery.
check("identical runs align", merge("पी. नं-बी 90, ख नं-701", "190-701", ["190-701"]),
      "पी. नं-बी 190, ख नं-701")
# Trailing loss as well as leading and middle.
check("trailing loss aligns", merge("एचएनओ 146", "1463", ["1463"]), "एचएनओ 1463")

print()
print("=== lookalike promotion ===")
# '॥' is a misread trailing 1. Paddle confirms a 3-digit run there, so the
# promotion is kept.
check("promote ॥ when Paddle agrees",
      merge("ख नं-70॥", "701", ["701"]), "ख नं-701")
# A lookalike with no confirming Paddle read is still promoted — the caller
# only reaches this with a Devanagari house value, and the lookalike is far
# more likely to be a real misread 1 than a genuine danda in a house field.
check("lookalike promoted even without Paddle runs",
      merge("पी. नं-बी 90, ख नं-70॥", "", []), "पी. नं-बी 90, ख नं-701")
# '॥' is two characters wide but stands for one lost digit.
check("double danda becomes one digit",
      merge("ख नं-70॥", "701", ["701"]), "ख नं-701")
check("no Devanagari lookalike string untouched",
      merge("नं 5", "5", ["5"]), "")

print()
print(f"{len(fails)} failure(s)" + ("" if not fails else ": " + ", ".join(fails)))
sys.exit(1 if fails else 0)
