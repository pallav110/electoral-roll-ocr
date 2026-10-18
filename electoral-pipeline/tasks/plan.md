# Implementation Plan: Dynamic OCR Robustness

## Overview

Make the electoral-roll OCR pipeline resilient to unfamiliar scan geometry,
resolution, sparse pages, blank card slots, duplicate PDFs, and unsupported
layouts without silently losing voters or fabricating values. The existing fixed
3x10 card lattice and field rules remain the fallback until the dynamic paths
prove zero missed populated cards on the 161-roll corpus.

The active batch is not interrupted by this work. Changes are built and verified
for future runs.

## Architecture Decisions

- **Shadow before authority:** dynamic page/card occupancy and lattice detection
  first run alongside the existing extractor, never changing records or skipped
  pages.
- **Three-way occupancy result:** `occupied`, `blank`, or `uncertain`. Only
  `occupied` is eligible for future full OCR; `uncertain` always uses the fixed
  fallback.
- **Blank-page safety:** a page is skipped only when page-level occupancy is
  confidently blank. Sparse pages remain processable.
- **No single-signal decisions:** empty ink runs, borders, photos, and scan noise
  are not enough to identify a blank card. Occupancy combines page-relative ink,
  EPIC-region ink, connected components, and optional cheap EPIC evidence.
- **Evidence-first values:** every future field decision must retain source
  engine/crop/confidence/review state. Unsupported values remain blank rather
  than being fabricated.
- **Content deduplication at discovery:** exact local PDF SHA-256 content is one
  canonical document; later identical paths are logged and skipped before
  sessions or units are created.
- **Performance normalization follows correctness:** no OCR work is skipped or
  resized authoritatively until shadow comparison proves coverage.

## Task List

### Phase 1: Duplicate and occupancy foundations

- [x] **Task 1: Land exact duplicate discovery guard**
  - Acceptance: same bytes under different filenames create one canonical
    `Document`; the second path creates no session or extraction units and is
    logged as a duplicate.
  - Verify: `pytest tests/test_duplicate_pdf_discovery.py`; run existing upload
    regression tests; inspect the database for one row per hash.
  - Files: `app/workflow.py`, `tests/test_duplicate_pdf_discovery.py`,
    `tests/test_upload_regressions.py`
  - Dependencies: None
  - Scope: Small

- [x] **Task 2: Add pure occupancy classifier**
  - Acceptance: classify synthetic full, sparse, blank, border-only, photo-only,
    noisy, and partial-tail card crops as `occupied`, `blank`, or `uncertain`;
    classification is scale-relative and never calls full OCR.
  - Verify: focused synthetic tests at multiple resolutions and image contrasts.
  - Files: `ocr_pdf_api.py`, `tests/test_card_occupancy.py`
  - Dependencies: None
  - Scope: Medium

- [x] **Task 3: Add page occupancy shadow instrumentation**
  - Acceptance: every processed page logs page classification, predicted
    occupied-slot count, fixed-grid slot count, and disagreement count; current
    records and page skipping behavior remain unchanged.
  - Verify: run two known fixtures and assert output records are byte-equivalent
    to the pre-shadow baseline; inspect structured logs.
  - Files: `ocr_pdf_api.py`, `tests/test_occupancy_shadow.py`
  - Dependencies: Task 2
  - Scope: Medium

### Checkpoint: occupancy safety

- [ ] Unit tests pass.
- [ ] S24 and Lansdown record counts are unchanged.
- [ ] No page is skipped because of the shadow detector.
- [ ] Every dynamic/fixed disagreement is visible in logs.

### Phase 2: Dynamic geometry and sparse-page coverage

- [x] **Task 4: Implement shadow lattice inference**
  - Acceptance: infer row pitch and phase candidates from page ink/periodicity;
    compare against the fixed 3x10 lattice; never use inferred rectangles for
    production extraction yet.
  - Verify: synthetic grids with changed scale/phase; both real fixtures;
    report false-positive internal card gaps as uncertain.
  - Files: `ocr_pdf_api.py`, `tests/test_lattice_shadow.py`
  - Dependencies: Task 2
  - Scope: Medium

- [x] **Task 5: Add per-card shadow occupancy comparison**
  - Verified on a real Lansdown sparse page: 14 predicted occupied slots,
    16 confident blanks, 14 fixed-extractor records, zero record-presence
    disagreements.
  - Production blank-slot skipping remains conservative; unknown layouts still
    use the fixed fallback.
  - Acceptance: compare dynamic occupied slots against records produced by the
    fixed extractor; report false blanks, false occupied slots, and page-level
    disagreements without changing output.
  - Verify: all available fixture pages plus synthetic blank/sparse/tail pages;
    zero missed populated cards in the accepted corpus.
  - Files: `ocr_pdf_api.py`, `tests/test_occupancy_shadow.py`,
    `tools/occupancy_audit.py`
  - Dependencies: Tasks 2–4
  - Scope: Medium

- [ ] **Task 6: Corpus shadow audit**
  - Acceptance: run the occupancy/lattice shadow audit across all 161 PDFs;
    produce JSON/CSV with per-page classification, predicted card count,
    fixed records, disagreements, and unknown-layout flags.
  - Verify: no silent crash; audit covers all PDFs and reports unreadable files;
    manually inspect every disagreement class before enabling authority.
  - Files: `tools/occupancy_audit.py`, `DOCS/docs/occupancy-audit.md`
  - Dependencies: Task 5
  - Scope: Medium

### Checkpoint: dynamic detector gate

- [ ] Zero missed populated cards across the 161-roll shadow audit.
- [ ] Sparse pages and partial tail pages are represented in tests.
- [ ] Any disagreement is either explained or remains `uncertain`.
- [ ] No authority switch until a human reviews the audit summary.

### Phase 3: Evidence-backed extraction

- [ ] **Task 7: Add field evidence records**
  - Acceptance: field candidates record engine, crop/region identifier,
    preprocessing variant, confidence, validation result, and review reason;
    public output remains backward compatible.
  - Verify: existing parser/output contract tests plus evidence serialization
    tests; no sensitive raw image data in logs.
  - Files: `ocr_pdf_api.py`, `tests/test_field_evidence.py`
  - Dependencies: Tasks 2–6
  - Scope: Medium

- [x] **Task 8 targeted: Harden age/house unresolved-value guards**
  - Existing review triggers, direct-card recovery, field sources, and blank-safe
    arbitration preserve values and flag unresolved age/house reads.
  - Full corpus validation remains open.
  - Acceptance: age/house values cannot be accepted from a crop that lacks the
    relevant label/row; disagreement leaves the value unchanged or blank and
    adds review evidence; all current S24/Lansdown examples are covered.
  - Verify: rendered-card regression fixtures, focused tests for fabricated age,
    mixed house reads, Devanagari digits, and blank crops.
  - Files: `ocr_pdf_api.py`, `tests/test_age_ink_guard.py`,
    `tests/test_house_number.py`
  - Dependencies: Task 7
  - Scope: Medium

- [ ] **Task 9: Enable dynamic occupancy only for proven classes**
  - Acceptance: only pages/card classes passing the corpus gate skip blank slots;
    uncertain/unknown pages use the fixed fallback; skipped slots carry an
    explicit reason; record counts do not decline.
  - Verify: A/B extraction on the 161-roll corpus; compare records, EPIC yield,
    names, relations, house, age, gender, deletion, and review flags.
  - Files: `ocr_pdf_api.py`, `tests/test_dynamic_occupancy_rollout.py`
  - Dependencies: Tasks 6–8
  - Scope: Large

### Phase 4: Performance and operator output

- [ ] **Task 10: Normalize large card renders**
  - Acceptance: measure text height and normalize large scans before repeated
    OCR; preserve fallback at native scale when measurement is uncertain; no
    accuracy regression.
  - Verify: same-command before/after timing on S24, Lansdown, and corpus
    samples; field-by-field comparison.
  - Files: `ocr_pdf_api.py`, `tests/test_adaptive_rendering.py`
  - Dependencies: Task 9
  - Scope: Medium

- [ ] **Task 11: Reduce redundant Paddle calls with field evidence gates**
  - Acceptance: skip only calls whose field already has validated independent
    evidence; preserve deleted-mark quorum and fallback paths; report calls
    saved and fallbacks used.
  - Verify: timing and complete field comparison; no new retry/error increase.
  - Files: `ocr_pdf_api.py`, `tests/test_paddle_gates.py`
  - Dependencies: Tasks 7–10
  - Scope: Medium

- [ ] **Task 12: Export review and completeness report**
  - Acceptance: Excel export contains records plus review/completeness sheets:
    missing fields, uncertain layout pages, skipped blank slots, duplicate
    hashes, failed units, and per-PDF record/page counts.
  - Verify: export tests and an end-to-end sample download.
  - Files: `app/`, `tests/`, relevant export module
  - Dependencies: Tasks 6–11
  - Scope: Medium

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Occupancy detector misses a populated card | Critical data loss | Shadow mode, `uncertain` fallback, zero-miss corpus gate |
| Dynamic lattice locks onto internal card whitespace | Critical wrong crops | Periodicity plus phase validation; fixed lattice fallback |
| Sparse pages are misclassified as blank | Critical data loss | Page blank only on combined evidence; test sparse/tail pages |
| Wider field crops merge adjacent rows | Wrong values | Label/row evidence and narrow field isolation |
| Lower render scale reduces OCR accuracy | Wrong fields | A/B field-level comparison before rollout |
| Three OCR readers overcommit CPU | Throughput regression | Measure CPU/reader balance; keep bounded worker budget |
| Existing full batch is interrupted | Lost/duplicated work | No live rollout until batch completes; raw-unit reuse remains enabled |

## Verification commands

```powershell
# Focused occupancy and evidence tests
python -m pytest tests/test_card_occupancy.py tests/test_occupancy_shadow.py tests/test_field_evidence.py -q

# Existing OCR/header/age/house regressions
python -m pytest tests/test_header_geometry.py tests/test_age_ink_guard.py tests/test_house_number.py -q

# Full suite with the project database configured
$env:TEST_DATABASE_URL='postgresql+psycopg://electoral:electoral@127.0.0.1:5433/electoral_test'
python -m pytest tests -q
```

## Open Questions

- Which 161-roll shadow disagreements require manual review before enabling
  dynamic skipping?
- What minimum page/card occupancy confidence should be accepted for authority?
- Which fields must be present for a row to count as a populated voter card?
- Should export include skipped blank slots, or only review-worthy uncertain slots?
