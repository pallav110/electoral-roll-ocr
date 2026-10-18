# OCR Adaptive Rendering, Vocabulary Layer, and Corpus Harness

**Date:** 2026-10-17
**Status:** Draft for review
**Author:** Claude (Opus 4.8)

---

## The problem this solves

Three defects, each found by measurement, each with the same root cause: a
constant in absolute units, fitted to one roll.

1. **Per-page time is 3.5x over target on a large-page roll.** Target is
   7-9s/page. Measured end-to-end against the live service:
   S24-53 = 14.7s/page, S28-40 Lansdown = 50.9s/page.
2. **Header fields come back empty on an unfamiliar template** — fixed by
   commits 391cdf2 and c7ac345, but by adding mechanism, not by removing the
   class of bug. Twelve more roll-specific spellings remain as prose in
   docstrings where no program can read them.
3. **Nothing measures completeness.** `_page_looks_like_voter_grid` counts
   EPICs *inside the hardcoded grid*, so an unfamiliar template reports empty
   and the page is silently skipped. 531 records from 660 positions is a 19%
   loss that reads as entirely normal.

Of 161 production rolls, an unknown number are affected by (3) right now.

---

## What was measured, and what was not

Baseline, live service, `POST /ocr/extract`, container with 14 workers:

| Roll | Pages | Service elapsed | Per page | Records |
|---|---|---|---|---|
| S24-53 | 3-8 | 88.28s | 14.7s | 180 |
| S28-40 Lansdown | 3-6 | 203.08s | 50.9s | 120 |

Per-stage cost, measured separately (Lansdown, page 4, 30 cards):

| Stage | Measured | Runs on | Effective |
|---|---|---|---|
| Tesseract, per card | 2.310s/card, ~28 calls/card | 14 threads | ~5s/page |
| Paddle stacked pass (serial+house+age) | 4.34s/page | main thread | 4.34s/page |
| Paddle deleted-mark vote | 3.56s/page, 4 readers/card | main thread | 3.56s/page |

**Tesseract dominates and scales with card pixel area.** Lansdown's cards are
689x1761px; S24's are roughly 250x700px. That is 10.4x the pixels for 3.5x the
page time. Measured text height is 86px on Lansdown and 21px on S24 -- both far
above the ~30px Tesseract wants, so the excess is pure cost.

Two claims in the existing documentation are **wrong** and were corrected by
this profiling:

- `CLAUDE.md` states the deleted-mark vote is "8 of the pipeline's 9 Paddle
  calls per card". It is **4**: `_serial_deleted_vote` has an exact early exit
  and 30/30 cards decided on the second bound after 4 readers.
- `CLAUDE.md` describes it as "the single most expensive OCR operation in the
  pipeline". It is **13% of Paddle cost**. An initial proposal to reduce the
  vote from 8 readers to 3 would have optimised the wrong thing.

This is the reason the plan leads with rendering rather than with the vote.

---

## Architecture

Four units, in dependency order. Each is independently shippable and
independently reversible.

```
  Unit 1  Adaptive page/card occupancy  <- no dependencies
  Unit 2  Adaptive card rendering       <- occupancy + scale measurements
  Unit 3  Lexicon data layer            <- no dependencies
  Unit 4  Corpus harness                 <- consumes Unit 3
          (lattice detection starts shadow-only inside Unit 1)
```

### Required sparse/blank-page behavior

A page is not a guarantee of 30 populated voter cards. The current extractor
pays for every grid position after a page passes its coarse probe, so a page
with 5 cards and 25 blank slots can cost nearly as much as a full page. This
must change without creating a silent-recall failure.

The occupancy detector has three outcomes for every page/card position:

| Result | Action | Safety rule |
|---|---|---|
| `occupied` | Run full OCR for that card | Normal extraction |
| `blank` | Return no record without Tesseract/Paddle | Only when all occupancy signals agree |
| `uncertain` | Use the existing fixed-grid fallback | Never skip on weak evidence |

At page level:

- A fully blank/non-voter page is rejected after a cheap low-resolution scan.
- A sparse page is accepted and only occupied positions are sent to full OCR.
- A page whose occupancy detector is uncertain is processed using all existing
  grid positions, preserving current recall.
- The detector runs in **shadow mode first**: it predicts occupied slots while
  the fixed extractor still runs, and every disagreement is recorded.
- It may become authoritative only after it has zero missed populated cards on
  the 161-roll corpus plus synthetic blank/sparse-page tests.

Occupancy signals should be independent and scale-relative:

1. ink density in the card crop after removing outer rules;
2. ink/components in the EPIC region;
3. a cheap EPIC-shape or printed-label signal;
4. page-level ink distribution to distinguish a blank page from a sparse one.

A single empty run is not sufficient evidence: photos, internal card gaps, and
scan artefacts can be blank inside a populated card. The detector must combine
signals and use the `uncertain` fallback.

The hardcoded 3x10 lattice remains the safety fallback. Dynamic geometry can
suggest positions, but no detected lattice may replace the fallback until
record yield, EPIC coverage, and disagreement checks pass on the corpus.

### Unit 1 — Adaptive card rendering

**Problem.** Card crops are rendered at a fixed 200 DPI. On a 2806pt page that
yields cards 3.5x larger in each linear dimension than a 842pt page, and
Tesseract cost scales with area. The extra resolution buys nothing: measured
text height is already 86px against a ~30px requirement.

**Approach.** Measure text height once per document from a single rendered page,
then choose the render scale that puts text at a target height. This is the
`TESSERACT_MIN_HEIGHT_PX = 260` idiom the house and EPIC readers already use,
applied to the card pass.

- Measure: ink-row runs on one 200 DPI page, take the modal run height as the
  text height. `_adaptive_window_for` already implements this and is reused.
- Choose: `dpi = clamp(round(200 * TARGET_TEXT_PX / measured_text_px), 100, 200)`
  where `TARGET_TEXT_PX = 45`. Never render above the current 200 DPI.
- Apply: one `fitz.Matrix` per document, reused for every page.

**Why this cannot lose records.** The scale only ever decreases. A card crop is
the same region of the same page at a different resolution; no card position
disappears. Coverage is verified per page by comparing record counts before and
after — a lower count fails the build.

**Expected.** Lansdown 50.9s -> ~12-15s/page. S24 unchanged (already near
target scale, so the clamp holds it at 200 DPI).

**Risk.** Under-reading. Mitigated by the coverage check and by never dropping
below 100 DPI.

### Unit 2 — Lexicon data layer

**Problem.** Label spellings, separators and filler tokens live as literals
inside a 7,101-line module. Adding a district's spelling today means editing
Python, understanding every caller, and reviewing a diff.

**Approach.** A new `ocr_lexicon.py` loads `lexicons/*.yaml` at import and
exposes frozen structures. The vocabulary is stored as **literal strings**,
not regex source, and the loader compiles:

```yaml
# lexicons/anubhag.yaml
label_words: ["अनुभाग", "नाम"]
separators: [":", ";"]
narrow_one_chars: ["॥", "।", "!", "|", "I", "l", "[", "]"]
```

The loader builds the character class by `re.escape`-ing each entry. Storing
patterns as regex strings was rejected: `[॥।!|Il\[\]]+` in YAML requires
quadruple escaping, and in OCR code a broken regex fails **silently** —
returning blank rather than raising. Literals make that class of bug
impossible by construction.

`HINDI_FIELD_LABELS`, `_ANUBHAG_LABEL_PREFIX`, the separators, and the
Devanagari filler-token list all become shipped defaults in YAML. No new
dependency: PyYAML 6.0.3 is already in the OCR image via PaddleOCR.

**Unknown-label surfacing.** When a header line is read successfully but parses
to nothing, the raw text is logged as a lexicon addition candidate. The system
then reports what to add instead of a human finding it via a UI screenshot.

**Rollback.** The YAML defaults are byte-identical to the current constants, so
removing the module restores today's behaviour exactly.

### Unit 3 — Corpus harness

**Problem.** 161 rolls and 2 PDFs on disk. One golden baseline. 29 ad-hoc
probe scripts. Exactly one test file exercises more than one real PDF.

**Approach.** `tools/corpus_sweep.py`, two tiers.

- **fast** (~1 min/roll): renders page 3 plus two spread pages, runs
  `_extract_roll_header_metadata`, counts EPIC-shaped reads per card position.
  No per-card Tesseract. Catches geometry and header breakage.
- **full**: complete extraction, for any roll the fast tier flags.

**Four checks**, none requiring ground truth:

| Check | Rule | Catches |
|---|---|---|
| Card yield | records / grid positions < 0.9 | the 531-of-660 silent loss |
| Header completeness | any of state/ac/booth/anubhag empty | header bugs |
| Field population | name/relation/EPIC below corpus median | scoring bugs |
| Timing | per-page outside 7-9s | oversampling regressions |

Card yield is applied **per roll, never per page**: tail pages legitimately
emit 14 and 7 records, so a per-page rule would flag every one of them.

Output is JSON plus a markdown table. **The table becomes the source the
documentation is written from**, so stale prose has a measured replacement.

### Unit 4 — Card-lattice detection (deferred)

Pitch is inferrable: autocorrelation of the row-ink profile returns **18.86%**
on both rolls — identical across a 3.7x linear scale difference. Phase is not
yet proven. Raw gutters are insufficient: Lansdown has a 523px empty column at
77.8-87.3% of page width, which is a photo box *inside* a card.

Deferred because it is the only change here that can **silently drop voters**
rather than return a blank field, and because Unit 3's measurements should tell
us which rolls actually need it. Ships shadow-mode first: computed, logged,
never used.

---

## Testing

- Unit 1: tests pin that coverage is unchanged (record count per page) and that
  the measured scale lands in range for both rolls. Verified by re-running
  extraction on both fixtures and comparing counts.
- Unit 2: tests pin that every YAML default compiles to exactly the current
  constant, that unknown labels surface, and that malformed YAML raises at
  import rather than returning blank.
- Unit 3: tests pin the four checks against synthetic inputs, including a
  deliberately short roll that must be flagged.
- Every unit is verified by running the full suite with and without the change
  and diffing the failure sets. Current baseline: 6 failing and 4 erroring test
  files, all pre-existing (`boto3` and `itsdangerous` missing from the venv).

## Documentation corrections

`CLAUDE.md` states the module is "~2980 lines". It is **7,101**. The deleted-mark
vote description is wrong as above. Both are corrected as part of Unit 3, along
with a reconciliation of the duplicated `DOCS/` (10 files at repo root, 6 in
`electoral-pipeline/DOCS/`).

## Corpus

The 161 rolls are not on this machine (`ingest/` empty, `sample-pdfs/` holds
one fixture and a 0-byte blank). The user is copying them here first. Unit 3's
sweep runs once they land.

## Success criteria

1. Both rolls complete in 7-9s/page, measured by the live service.
2. Record counts per page identical to the pre-change baseline.
3. The harness produces a pass/fail row for every roll in the corpus.
4. No new test failures against the recorded baseline.
5. A district spelling is addable as YAML, with no Python change.