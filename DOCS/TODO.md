# OCR Pipeline TODO

## Priority 0 — Correctness and data safety

- [x] Preserve a valid Tesseract serial number when PaddleOCR returns an empty serial.
  - Added `_choose_serial_value()` and covered the fallback in the built-in regression path.
  - Target: `scripts/ocr_pdf_api.py:2123`
  - Verify with Paddle unavailable/failing and a card whose Tesseract output contains a serial.

- [x] Make voter-page detection tolerant of OCR failure in the first three cards.
  - `_page_looks_like_voter_grid()` now probes representative cards across all four page bands and columns.
  - A valid EPIC in a later row can keep the page from being skipped.

- [ ] Re-run the Page 20 end-to-end audit after the latest serial-496 focused-name fix.
  - [x] Saved `page20_api_response_todo_phase1_20260926.json` and `page20_json_diff_todo_phase1_20260926.json` without overwriting historical fixtures.
  - [x] Confirmed 30 records, serials 495–524, serial 497 `इ-10/602`, serial 522 `इ-15/245`, no mojibake/photo leakage, and boolean `is_deleted`.
  - [ ] Serial 496 still returns the stray `हु`; focused-name arbitration requires another fix and rerun.

- [ ] Review and resolve remaining Page 20 field mismatches against `manual_check_record.json`.
  - [x] Generated a fresh field-level diff: 16 exact matches, 14 mismatched records, 28 field mismatches.
  - [x] Classified the current run as structural/OCR mismatches rather than changing ground truth; literal UTF-8 and boolean `is_deleted` checks pass.
  - [ ] Resolve the remaining mismatches without card-specific runtime overrides.

## Priority 1 — Security and batch reliability

- [x] Close the batch path time-of-check/time-of-use race.
  - Worker revalidates the resolved source path against `OCR_BATCH_ALLOWED_ROOTS` before reading.
  - Targets: `/ocr/batch/path` and `_run_pdf_batch()`.
  - Revalidate the resolved source path in the worker, or stage/copy accepted files into the job directory before queueing.

- [ ] Restrict production network exposure.
  - CORS is now configurable via `OCR_API_CORS_ORIGINS`; authentication and default-bind policy remain pending.
  - Review the default `0.0.0.0` bind address.
  - Add authentication/authorization before exposing OCR or batch endpoints beyond a trusted local environment.
  - Replace wildcard CORS with an explicit trusted-origin configuration.

- [x] Sanitize uploaded batch filenames for Windows and POSIX filesystem rules.
  - Added shared `_safe_filename()` handling separators, invalid characters, reserved Windows names, and trailing dots/spaces.
  - Reuse one safe filename helper for upload staging and output names.
  - Cover reserved names, invalid characters, trailing dots/spaces, and path separators.

- [ ] Remove partial batch-upload directories when staging fails.
  - [x] Failed upload staging now removes the partial job directory.
  - [x] Clean up files when per-file or total-byte limits reject an upload.
  - [ ] Add retention/cleanup for abandoned and completed job directories.

- [x] Avoid returning absolute server filesystem paths in public API responses.
  - `json_output_file` now returns only the generated filename; the server still writes to its configured internal output directory.

## Priority 2 — OCR arbitration and maintainability

- [x] Preserve `focused_prefix` in the slash-form denominator-repair branch.
  - Denominator repair now falls back to focused prefix evidence when Tesseract has no prefix.
  - Target: `scripts/ocr_pdf_api.py:1882-1883`.
  - Add a regression test for a focused short-i prefix during denominator repair.

- [x] Decide whether `_extract_tesseract_house_address()` should be wired into extraction.
  - It is now used only when the primary house value is missing or contains a structured-address marker; a complete primary value is never overwritten.

- [ ] Add regression coverage for the senior-review findings.
  - [x] Serial fallback when Paddle returns no serial is covered by the built-in self-test.
  - [x] Focused house-prefix preservation is covered by the built-in self-test.
  - [ ] Add isolated tests for first-row EPIC probe failure, batch filename sanitization, and failed upload cleanup.
  - Existing 72 built-in checks continue to cover house arbitration, relation labels, photo filtering, and focused-name behavior.

- [ ] Audit broad house-prefix normalization for legitimate numeric hyphenated addresses.
  - Review mappings such as `8-...` and `5-...` to `इ-...`.
  - Require independent prefix evidence where needed.

## Priority 3 — Performance architecture (after correctness is stable)

- [ ] Replace per-card PDF rasterization with one full-page render and NumPy card slicing.
  - [x] Implemented an opt-in two-DPI full-page renderer and card slicing using `_voter_card_rects()` (`OCR_FULL_PAGE_RENDER=true`).
  - [x] Kept Hindi OCR at 200 DPI and metadata OCR at 300 DPI.
  - [ ] Validate output parity before making full-page rendering the default; the first benchmark showed serial drift because the encoded slices do not yet match the original clip rasterization exactly.
  - [ ] Measure the isolated rendering win after crop-boundary parity is fixed.

- [ ] Reintroduce controlled concurrent card OCR only after correctness validation.
  - [x] Added configurable `OCR_CARD_WORKERS` (safe default `1`; increase only after a correctness benchmark) and ordered card result collection.
  - [x] Paddle initialization and inference remain protected by explicit locks; cross-process OCR serialization is preserved.
  - [ ] The attempted concurrent sliced run reached 42.66 seconds but produced 25 missing serials; it is rejected as a correctness result and must not be used as the default.
  - [ ] Validate concurrency against a correctness-preserving render path and benchmark worker counts; target approximately 60–70 seconds per page.

- [ ] Add timing comparisons to the Page 20 audit.
  - [x] Existing API output records request/page elapsed values and per-card stage logs.
  - [ ] Add the post-change benchmark result and compare it with the 138.95-second baseline after the run completes.

## Verification checklist

- [x] `python scripts/ocr_pdf_api.py self-test` — passed with 72 checks.
- [x] `python -m py_compile scripts/ocr_pdf_api.py` — passed.
- [x] `pytest -q tests/test_photo_mask.py` — 2 passed.
- [x] `git diff --check` — passed.
- [x] Page 20 response contains exactly 30 records and serials 495–524.
- [x] No escaped Unicode or mojibake markers appear in the response.
- [x] No `फोटो उपलब्ध है` text leaks into voter fields.
- [x] Every `is_deleted` value is a JSON boolean.
- [x] Historical Page 21 fixtures were not overwritten by the new response/diff files.
