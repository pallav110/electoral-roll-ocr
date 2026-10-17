"""Hypothesis test, minimal and single-variable.

  H: _split_anubhag_label() only splits on ':' or ';'.  A roll that prints the
     section line as 'अनुभाग संख्या व नाम ॥-कोचियार' -- label, hyphen, value, with
     NO colon -- therefore keeps the label, and both anchored parsers below it
     then reject the line.  With the label removed, the existing parsers already
     handle the value correctly and nothing else needs to change.

This changes no code.  It calls the current functions on both the broken and the
repaired input and reports what each does, so the claim is measured rather than
argued.
"""
import sys

sys.stdout = open("/data/hysep_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import re
import unicodedata
import ocr_pdf_api as A

# The two lines exactly as Tesseract returns them from the measured roll.
LINES = (
    ("Lansdown S28-40", "अनुभाग संख्या व नाम ॥-कोचियार"),
    ("S24-53 (works)", "अनुभाग संख्या और नाम : -उत्तरांचल कालोनी गली न0 8 से 9"),
    ("HIN-116 shape", "अनुभाग संख्या और नाम; 4-गढी"),
)

# The proposed repair: when no colon/semicolon separator is present, drop the
# printed label itself and everything up to the value's own leading dash.
LABEL = re.compile(r"^[^:;\n]{0,60}?नाम\s*(?:[:;]\s*)?", re.UNICODE)


def split_with_label_fallback(text: str) -> str:
    after = A._split_anubhag_label(" ".join(text.split()))
    if after != " ".join(text.split()):
        return after                      # a real ':' or ';' separator was used
    stripped = LABEL.sub("", after, count=1)
    return stripped or after


print("=" * 78)
print("CURRENT behaviour")
print("=" * 78)
for label, line in LINES:
    print(f"\n{label}")
    print(f"  input      = {line!r}")
    print(f"  split      = {A._split_anubhag_label(line)!r}")
    print(f"  code       = {A._anubhag_code_from_header_text(line)!r}")
    print(f"  name       = {A._clean_anubhag_name_candidate(line)!r}")

print("\n" + "=" * 78)
print("WITH the label-fallback split (nothing else changed)")
print("=" * 78)
for label, line in LINES:
    after = split_with_label_fallback(line)
    print(f"\n{label}")
    print(f"  input      = {line!r}")
    print(f"  split      = {after!r}")
    print(f"  code       = {A._anubhag_code_from_header_text(after)!r}")
    print(f"  name       = {A._clean_anubhag_name_candidate(line)!r}")

# Does the fallback ever damage a line that already worked?  It returns the
# original untouched whenever a ':' or ';' separator exists, so those are
# unchanged by construction -- verify rather than assert.
print("\n" + "=" * 78)
print("REGRESSION GUARD -- lines that already split correctly")
print("=" * 78)
for line in LINES[1:]:
    cur = A._split_anubhag_label(" ".join(line.split()))
    new = split_with_label_fallback(line)
    status = "unchanged" if cur == new else "CHANGED"
    print(f"  {status:>9}  {line[:52]!r}")

# And a line with no label and no separator must survive untouched, or a bare
# locality read from the narrow crop would be mangled into nothing.
print("\n" + "=" * 78)
print("GUARD -- bare values with no printed label")
print("=" * 78)
for bare in ("प्रगति विहार", "सरस्वती नगर", "॥-लक्ष्मी इन्कलेव 7"):
    print(f"  {bare!r:28} -> {split_with_label_fallback(bare)!r}")