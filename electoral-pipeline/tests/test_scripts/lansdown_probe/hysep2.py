"""Hypothesis test v2 -- the label-aware split, plus its adversarial cases.

v1 confirmed the mechanism but its regex was too loose: it matched on 'नाम'
alone, which would also strip a bare locality that happens to contain that word
('नाम का पेड़').  v2 anchors on the printed label's own first word, अनुभाग, so a
value is only ever stripped when it really is preceded by the label.

Cases marked MUST-PRESERVE are the ones a loose regex would damage.  They come
from real header shapes and from `_extract_anubhag_name_from_header_fallback`,
which passes already-bare values through this same function.
"""
import sys

sys.stdout = open("/data/hysep2_out.txt", "w", encoding="utf-8", errors="replace")
sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, "/app")

import re
import ocr_pdf_api as A

# Anchored on अनुभाग -- the label's own first word -- and non-greedy up to the
# first नाम after it.  A separator, if printed, is consumed too.
LABEL = re.compile(r"^\s*अनुभाग.*?नाम\s*[:;]?\s*", re.DOTALL)


def split_v2(text: str) -> str:
    joined = " ".join(str(text or "").split())
    after = A._split_anubhag_label(joined)
    if after != joined:
        return after                       # a real ':' / ';' separator was printed
    return LABEL.sub("", after, count=1) or after


# (label, line, expect_code, expect_name)
CASES = (
    # --- the measured failures -------------------------------------------
    ("Lansdown S28-40", "अनुभाग संख्या व नाम ॥-कोचियार", "1", "कोचियार"),
    # --- already working; must not change --------------------------------
    ("S24-53 colon", "अनुभाग संख्या और नाम : -उत्तरांचल कालोनी गली न0 8 से 9",
     "", "उत्तरांचल कालोनी गली न0 8 से 9"),
    ("HIN-116 semicolon", "अनुभाग संख्या और नाम; 4-गढी", "4", "गढी"),
    ("HIN-45 danda", "।-प्रगति विहार", "1", "प्रगति विहार"),
    ("HIN-403 danda", "॥-सरस्वती", "1", "सरस्वती"),
    ("HIN-120 digit", "7-लक्ष्मी इन्कलेव", "7", "लक्ष्मी इन्कलेव"),
    ("HIN-128 no digit", "प्रगति विहार", "", "प्रगति विहार"),
    # --- MUST-PRESERVE: bare values with no printed label -----------------
    ("bare w/ नाम inside", "नाम का पेड़ विहार", "", "नाम का पेड़ विहार"),
    ("bare, no label", "उत्तरांचल कालोनी गली न0 8 से 9", "", "उत्तरांचल कालोनी गली न0 8 से 9"),
    ("bare locality", "सरस्वती नगर", "", "सरस्वती नगर"),
    ("bare अनुभाग word mid", "गढ़ी अनुभाग विहार", "", "गढ़ी अनुभाग विहार"),
)


def report(tag: str, fn) -> None:
    print("=" * 78)
    print(tag)
    print("=" * 78)
    bad = 0
    for name, line, want_code, want_name in CASES:
        after = fn(line)
        got_code = A._anubhag_code_from_header_text(after)
        got_name = A._clean_anubhag_name_candidate(line)
        ok = (got_code == want_code and got_name == want_name)
        bad += not ok
        print(f"\n  {'PASS' if ok else 'FAIL'}  {name}")
        print(f"        line     = {line!r}")
        print(f"        split    = {after!r}")
        print(f"        code     = {got_code!r}   want {want_code!r}")
        print(f"        name     = {got_name!r}")
        print(f"                   want {want_name!r}")
    print(f"\n  {len(CASES) - bad}/{len(CASES)} pass\n")
    return bad


before = report("v1 -- CURRENT _split_anubhag_label", A._split_anubhag_label)
after_bad = report("v2 -- label-aware split", split_v2)

# The decisive check: which cases changed behaviour at all?
print("=" * 78)
print("WHAT CHANGED -- only a genuinely label-prefixed line may differ")
print("=" * 78)
changed = 0
for name, line, _wc, _wn in CASES:
    cur = A._split_anubhag_label(" ".join(line.split()))
    new = split_v2(line)
    if cur != new:
        changed += 1
        print(f"  changed  {name:22} {cur!r} -> {new!r}")
    else:
        print(f"  same     {name}")
print(f"\n  {changed} of {len(CASES)} lines altered")
print(f"  v1 failing: {before}   v2 failing: {after_bad}")