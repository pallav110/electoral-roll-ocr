# Ground truth — human-verified voter records

The reference set every accuracy claim in this repo is measured against. These records
were read off the PDF **by a human** and are treated as authoritative over pipeline output
when the two disagree.

## What's here

| File | What it is |
|---|---|
| `whole_pdf_6-8_results.json` | **The reference set.** 90 verified records covering PDF pages 6–8. `records_needing_review: 0` — every field was checked. |
| `json_files/page<N>_results.json` | The same data split per page (3–8, 21). Used by the per-page comparison scripts. |

The roll is `2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf`, which lives in
`sample-pdfs/`.

## Why this folder is gitignored

The records are **derived from a real electoral roll**, so they are excluded from version
control deliberately — see `.gitignore`. That has a consequence worth stating plainly:

**This folder is not in git. If you delete it, it is gone.** There is no `git checkout`
that brings it back. Copy it somewhere else before you clean up, and back it up before you
sell or hand the project over.

## What depends on it

- `tests/test_singh_glyph_corrections.py` — reads the reference set to prove the सिंह
  corrections cannot rewrite a name a human confirmed. **Skips** (does not fail) if the
  file is missing, so a green run can hide it.
- `tests/check_ui_hindi_english.py` — display-only check that the UI renders both languages.

## Measured accuracy

Scored by matching on **EPIC, never on row position** — serials repeat across booths, but
an EPIC cannot collide between two voters.

| Measure | Result |
|---|---|
| Field-level | **99.7%** (923 of 926 comparable fields) |
| Whole-record exactness, all 21 fields | **96.7%** (87 of 90 records) |

Alignment caveat: `whole_pdf_6-8_results.json` carries one extra leading row — the tail
card of page 5 — so row *N* of the reference set is card *N+1* of page 6. Matching on
position instead of EPIC silently misaligns the whole comparison.

## Regenerating it

The per-page scratch scripts that originally produced these lived in a folder that has
since been removed. They were one-off and are not recoverable from git. If you need to
re-verify against a different roll, re-derive the records by hand from the PDF — that is
what "human-verified" means here, and a regenerated-by-machine file would not carry the
same authority.
