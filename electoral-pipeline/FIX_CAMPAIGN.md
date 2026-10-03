# Fix campaign — tracker

Scope: **Tiers 0–3 (~30 of 38 review items)**, per your decision. Tier 4
(file-splitting, typing, doc rewrite) is explicitly out.

Working rule for this whole campaign: **verify by executing.** Two fixes in this
campaign were correct on paper and broke the build. One of them — failing
`config.py` closed on `ADMIN_TOKEN` — turned 238 green tests into collection
errors, because the raise happens at *import* time and pytest imports test
modules before any fixture runs.

Two verification tiers:
- **app-side** → full `pytest` suite (fast, ~3s). Run before every commit.
- **`ocr_pdf_api.py`** → full 22-page re-roll diffed against the golden baseline
  (531 records, sha256 `950afacd104df01eac083439b0459dde2c47085cfee745a1d28ddf5d21fd1cf4`,
  `C:\tmp\ocr_golden.json`). ~25 min per pass, so these batch **one pass per
  commit**, not one per finding.

---

## Legend
`[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Tier 0 — data corruption and permanent strandings

- [x] **T0-1** Scope every read and export path to one extraction attempt
  (`app/main.py:174`, `:411`, `:693`) → `c76c6e7`. New `app/record_scope.py`
  owns the choice so four call sites can't drift. Rule: a completed/partial
  attempt wins over a *later* failed one — "newest session wins" is simpler and
  wrong, it discards a good extraction for a retry that crashed. Export gains an
  `attempt` column. 7 new tests. **Correction to the note below: the review's
  premise was right** — the constraint exists and the reads genuinely were
  unscoped; what I got wrong first time was *how* the duplication arises.
  - CORRECTION (kept for the record): my earlier "the review was wrong about the
    constraint" note was itself wrong. `uq_electoral_record_source UNIQUE btree
    (session_id, page_number, source_row_number) WHERE ...` **does** exist in the
    live DB. The `on_conflict_do_nothing` at `workflow.py:780` targets that same
    index, which makes idempotency **per-attempt** rather than per-document. A
    retry gets a new session id, so it does not collide and inserts duplicates
    alongside the old attempt. Latent today: 531 rows in one session, five empty.
- [x] **T0-2** `locked_at`/`locked_by` dead branch (`workflow.py:361`) → `8b64ac3`.
  `dispatch_documents()` now stamps both on publish, so `recover()`'s republish
  condition compares a real deadline instead of NULL. Two *further* defects
  surfaced while testing — the branch's whole recovery loop was dying on a
  `TypeError` (`(a or b) < cutoff` raises when both are NULL; four such units
  were live), which rolled back every republish the beat had queued. Three
  defects, not one.
  **The test nearly shipped vacuous.** Hand-building the fixture rows meant it
  set `locked_at` itself, so all 5 tests passed with the publish stamp removed.
  Rewritten to drive the real `dispatch_documents()` with a swallowing broker;
  `_age_the_publish()` now refuses to run on a NULL `locked_at` so it cannot
  become the source of the value under test. Verified: 5 pass with, 2 fail without.
- [x] **T0-3** Invert confidence arithmetic (`_compute_confidence`, `:2516`;
  call site `:2667`) — separate "no evidence" from "zero confidence", count
  zero-hit fields as 0.0, emit a coverage term · **OCR pass pending**
  Zeros are evidence. PaddleOCR records an unreadable field as 0.0
  (`field_confidences[field] = ... if field_confs else 0.0`, `:2973`), and the
  old code filtered exactly those out before averaging —
  `[c for c in paddle_conf.values() if c > 0]` — which rewarded failure twice:
  a miss stopped contributing, and an all-zero dict became an empty list, so
  `paddle_avg = None` and bare `tess_norm` returned **at full weight**. Measured
  on the frozen 531: every record scored 0.7374–0.9660, mean 0.9138, zero
  `None`. A score that cannot drop below 0.74 on blank tail pages and
  struck-through deletions is not measuring what it claims.
  Now returns `(confidence, coverage)` and emits both. Two latent bugs surfaced
  while writing the tests, both from `app/normalize.py:58-65`'s documented
  lesson: `max(0.0, min(1.0, nan))` yields **0.0** in CPython (`min` returns
  its first argument when the comparison is False), and `min(1.0, inf)` yields
  **1.0** — the *maximum* confidence from a producer that emitted infinity.
  NaN/inf are now rejected before the clamp. 9 tests in
  `tests/test_confidence_coverage.py` call the real function; three of my own
  expected values were arithmetically wrong and running them found the inf bug.
- [x] **T0-4** `Index("idx_records_document", "document_id", "page_number")`
  (`models.py:251`) → `8f59efc`. Measured: Seq Scan + Sort → Index Scan +
  Incremental Sort, **0.614 ms → 0.179 ms**. Index applied to the live DB too.
  Note: `create_all()` adds tables but never an index to an existing table — the
  same gap `migrate.py` covers for columns, still open for indexes.
- [ ] **T0-5** Total deterministic keys on every arbitration `max()`
  (`ocr_pdf_api.py:888-889`, `:986`, `:1190`, `:1211`, `:1597`, `:2860`); extend
  `:888` to flag `_needs_review` the way `:944` does · **OCR pass**
- [x] **T0-6** `roll_year` no longer a hardcoded 2026 (`extractor.py`) → `81b7d72`.
  Two instances, not one — the test caught the `mock_extract` copy. Now read from
  `roll_metadata`, NULL when absent. The 16 already-stored rows keep their value.
- [ ] **T0-7** Read `translit_source_hash` in `_needs_backfill` (`migrate.py:139`)
  as a separate Python pass; make `backfill_english` fail loudly instead of
  rewriting the same 500 rows forever
- [x] **T0-8** `map_record` survives non-numeric `confidence`; `ExtractionError`
  re-raised before the chain; `normalize_unit` reports written not intended →
  `18830d4`. Four defects found, not one:
  - `float(confidence)` raised on `'high'`/`''` — in a list comprehension over a
    unit, so **one bad record aborted all 30**
  - `bool` passed the range check (`0 <= True <= 1`) and was **stored** as confidence
  - out-of-range was flagged but **stored as-is** into `Numeric(5,4)`
  - `inf` clamps to `1.0` — caught by the test *after* my first fix shipped it
  - `rowcount` under `ON CONFLICT DO NOTHING` reports rows processed, not
    inserted → verified against the live DB, switched to `RETURNING`
  - `written` shipped once **without its initialiser**; 308 tests stayed green
    because nothing ran the insert path. Hence `test_normalize_unit_db.py`.

## Tier 1 — security

- [x] **T1-10** `compare_digest` TypeError + fail-closed `ADMIN_TOKEN` → `69f70b9`
- [x] **T1-11** `package_for_team.py` allowlist → `5716a19`
- [ ] **T1-9** Authenticate `/ocr/extract` against `EXTRACTOR_API_KEY` with
  `secrets.compare_digest`; refuse to start when empty. The repo's own client
  already sends the header.
- [ ] **T1-12** Bound `_resolve_scan_path`: reject `..` segments, reject the
  filesystem root, `posixpath.normpath`, assert under an allowed prefix, return
  400. Fix `main.py:256` to *use* the value it computes instead of only logging.
- [ ] **T1-13** CSRF tokens + security headers on mutating routes;
  `restart: unless-stopped` + memory limit on `ocr`
- [ ] **T1-14** Retention policy + `0600` for `OCR_OUTPUT_DIR`; add `input`,
  `debug_cards`, `ingest`, `OCR`, `*.pdf`, `*_export.csv` to `.dockerignore`

## Tier 2 — recover the lost performance (deletions first)

- [ ] **T2-15** Remove the duplicated house-number read
  (`ocr_pdf_api.py:2186`/`:2188`) — 1–2 subprocesses per card, zero behaviour
  change · **OCR pass**
- [ ] **T2-16** Delete `_cl_for_deleted` (`:2264`, `:2754`) — ~60 MB/page of
  images nobody reads · **OCR pass**
- [ ] **T2-17** Stream both CSV exports; require an explicit filter plus a hard
  row cap on the unfiltered export; delete the dead `/browse/export` duplicate
- [ ] **T2-18** Batch `normalize_unit`'s inserts — 531 round-trips → 1–2; move
  `finalize()` from per-unit to per-session
- [ ] **T2-19** Add `idx_events_created` + partial `idx_docs_inflight`;
  `.limit(200)` on both `recover()` queries; compute the browse search clause once
- [ ] **T2-20** Stat before hashing in `_discover_under`; batch the scan into one
  transaction
- [ ] **T2-21** Fix double-logging (`app_log.propagate = False` or drop
  `humanlog.py`); delete `map_record`'s per-record prints; configure the OCR
  service's logger · **OCR pass** (partly)
- [ ] **T2-22** Re-point `_extract_card` at NumPy arrays — eliminate the
  660-per-roll PNG encode/decode round-trip · **OCR pass**

## Tier 3 — make the next change safe

- [x] **T3-23** `pyproject.toml` (pytest/ruff/mypy) → `5922c4e`
- [x] **T3-24** GitHub Actions workflow → `5922c4e`
- [x] **T3-28** `tests/conftest.py`, single `sys.path.insert`, autouse config
  restore → `5922c4e`
- [ ] **T3-25** Fix the two tests that assert nothing —
  `test_name_lexicon.py:57` (per-case expected outputs) and
  `test_house_number.py:48` (ordered digit *runs*, not sets). Then the five
  ranked missing cases from §2.1, starting with non-numeric confidence and
  `_safe_name` collisions.
- [ ] **T3-26** Repoint the broken `OCR/tests/` at `parents[2]/"ocr_pdf_api.py"`
  or move those two into `tests/`; `.gitignore` negation for
  `OCR/ground_truth_per_page_values_verified/`; move `test_merge_house.py` into
  tracked `tests/`
- [ ] **T3-27** Make the scheme-contract test environment-independent —
  `monkeypatch.delenv("TRANSLITERATION_SCHEME")` in
  `test_default_scheme_is_itrans`, plus assert code default == shipped `.env`
- [ ] **T3-29** Extract `_compute_confidence` (pure, no OCR stack) and add the
  missing clamp — `tesseract_conf=150` currently returns 1.5. Then pull the
  inline house-number selection at `:2230-2257` into a pure helper so the
  Devanagari-wins and slash-2-agreement rules are table-testable.
  *CLAUDE.md's "treat `_choose_house_number` as absent" still holds — this
  extracts existing inline code, it does not reintroduce it.*
- [ ] **T3-30** Convert the calibration comments into fixtures: crop page 16
  cards 11 and 20 plus the five known deletions, assert 8/8 and 5/8. Make the
  5-vote threshold and the upscale-before-threshold ordering named module-level
  constants. **Highest-leverage gap in the project** — a refactor that reorders
  preprocessing, changes DPI, or shifts a crop boundary currently fails no test,
  it just silently reintroduces two missed deletions.

---

## Closed

- Deleted `C:tmptest_export.csv` (379,890 bytes) — authorized 2026-10-03.
- `ADMIN_TOKEN` / `HF_TOKEN` exposure — user confirmed not a concern.

## Golden baseline

531 records · sha256 `950afacd104df01eac083439b0459dde2c47085cfee745a1d28ddf5d21fd1cf4`
· `C:\tmp\ocr_golden.json` · builder `C:\tmp\golden_snapshot.py`

Unchanged since it was frozen — every OCR-file fix below must diff against it.