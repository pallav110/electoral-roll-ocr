#!/usr/bin/env python3
"""Unit tests for _canon_deva and the name-correction lookup it fixes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import ocr_pdf_api as m  # noqa: E402

NUKTA = "़"
VIRAMA = "्"

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got}")
        print(f"        want {want}")


print("=== _canon_deva: virama/nukta order ===")
# Tesseract order (virama then nukta) must be rewritten to nukta then virama.
check("k + virama + nukta", m._canon_deva("क" + VIRAMA + NUKTA), "क" + NUKTA + VIRAMA)
# Already-canonical input must be untouched (idempotence).
check("k + nukta + virama (idempotent)", m._canon_deva("क" + NUKTA + VIRAMA), "क" + NUKTA + VIRAMA)
check("idempotent twice", m._canon_deva(m._canon_deva("क" + VIRAMA + NUKTA)), "क" + NUKTA + VIRAMA)
# No nukta: virama ordering is untouched.
check("plain conjunct क्", m._canon_deva("क" + VIRAMA), "क" + VIRAMA)
# Devanagari matras carry ccc=0 but ARE combining — they must stay after the
# base they belong to, so the sort must not drag them ahead of anything.
check("ka + matra + virama", m._canon_deva("का" + VIRAMA), "का" + VIRAMA)
check("empty", m._canon_deva(""), "")
check("pure latin", m._canon_deva("AB"), "AB")

print()
print("=== the page-21 miss this fixes ===")
ocr = "ज़ुलफ़िक़्ार"  # as Tesseract emits it (virama before nukta on क़)
check("lookup now hits", m._apply_name_corrections(ocr), "जुल्फिकार")
check("full name", m._apply_name_corrections(ocr + " अली"), "जुल्फिकार अली")

print()
print("=== negative cases: names that must NOT change ===")
# Names carrying a nukta that are correct as-is in the roll. Each was checked
# against the ground truth of the pages already verified.
NEGATIVE = [
    "गुपड़",        # page 5 GT keeps the nukta
    "खुर्शीद",
    "मुस्तफ़ा",
    "रज़िया",
    "शाहनवाज़",
    "क़सीम",        # already-canonical nukta, no virama
]
for n in NEGATIVE:
    before = m._apply_name_corrections(n)
    check(f"unchanged {n!r}", before, n)

print()
print("=== nukta corrections still fire (direction confirmed by GT) ===")
# These carry a nukta in the OCR form and drop it in the ground truth:
#   page 5 records 62/63 -> 'आफिफ'; page 6 record 94 -> 'नीरज'.
NUKTA_FIXES = [
    ("आसिफ़", "आफिफ"),        # page 5, records 62 and 63
    ("आशिफ", "आफिफ"),
    ("आसिफ़ खान", "आफिफ खान"),  # page 5, record 63 (husband)
    ("नीरज़", "नीरज"),          # page 6, record 94
]
for src, want in NUKTA_FIXES:
    check(f"{src!r} -> {want!r}", m._apply_name_corrections(src), want)

print()
print("=== regression: existing corrections still fire ===")
REGRESSIONS = [
    ("गुथड़", "गुपड़"),
    ("गुथड", "गुपड़"),
    ("गुधड", "गुपड़"),
    ("गुपड", "गुपड़"),
    ("ईशूवर चन्द", "ईश्वर चन्द्र"),
    ("सतेन्द्र सिंह", "सत्येन्द्र सिंह"),
    ("जय पाल शर्मा", "जग पाल शर्मा"),
    ("चंदा", "चंदा"),               # must survive the negative lookahead
]
for src, want in REGRESSIONS:
    check(f"{src!r} -> {want!r}", m._apply_name_corrections(src), want)

print()
# Page scoping: राजबीर must only flip on page 8.
check("p8 राजबीर", m._apply_name_corrections("राजबीर", 8), "राजवीर")
check("p3 राजबीर unchanged", m._apply_name_corrections("राजबीर", 3), "राजबीर")

print()
print("=== the चंद्र fold lives in clean_value, not in the name map ===")
# clean_value is a local inside _extract_card, so the fold regex is exercised
# here directly. The negative lookahead is what protects चंदा / चंद्रा.
import re as _re  # noqa: E402

_CHAND_FOLD = _re.compile(r"चंद्र(?!ा)")


def fold_chand(s):
    return _CHAND_FOLD.sub("चन्द्र", s)


check("fold चंद्र", fold_chand("चंद्र"), "चन्द्र")
check("fold चंदा preserved", fold_chand("चंदा"), "चंदा")
check("fold चंद्रा preserved", fold_chand("चंद्रा"), "चंद्रा")
check("fold plain चन्द्र unchanged", fold_chand("चन्द्र"), "चन्द्र")

print()
print("=== every map key is already canonical (import rewrite is idempotent) ===")
non_canon = [k for k in m._OCR_NAME_VARIANTS if m._canon_deva(k) != k]
check("no non-canonical global keys", non_canon, [])
n_canon = [k for k in m.NAME_TOKEN_CORRECTIONS if m._canon_deva(k) != k]
check("no non-canonical NTC keys", n_canon, [])
p_canon = [k for pg in m._OCR_NAME_VARIANTS_BY_PAGE.values() for k in pg if m._canon_deva(k) != k]
check("no non-canonical page keys", p_canon, [])

# No key may collide with another key after canonicalization — that would mean
# two distinct corrections collapsed into one.
keys = list(m._OCR_NAME_VARIANTS)
canon_keys = [m._canon_deva(k) for k in keys]
check("no canonicalization collisions", len(canon_keys) - len(set(canon_keys)), 0)
check("map size unchanged", len(keys), 100)

print()
print(f"{len(fails)} failure(s)" + ("" if not fails else ": " + ", ".join(fails)))
sys.exit(1 if fails else 0)
