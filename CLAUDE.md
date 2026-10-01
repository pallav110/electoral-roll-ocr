# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Electoral-roll OCR pipeline that extracts structured voter records from Hindi PDF voter lists. Single-file FastAPI service (`electoral-pipeline/ocr_pdf_api.py`, ~2980 lines) containing all extraction logic — no local imports.

## Commands

```bash
# Start API server (default 0.0.0.0:8082)
python electoral-pipeline/ocr_pdf_api.py

# Docker stack (the service runs in a container)
docker compose up -d
docker compose cp electoral-pipeline/ocr_pdf_api.py ocr:/app/ocr_pdf_api.py
```

Note: paths in this file are repo-relative. The service itself lives in `electoral-pipeline/`, not `scripts/`.

## System Dependencies

Tesseract with Hindi language data must be installed: `tesseract-ocr` and `tesseract-ocr-hin`. Python deps in `ocr_api_requirements.txt`.

## Architecture

**Dual OCR engine design**: Tesseract handles Hindi+English text (voter names, relations, addresses) at 200 DPI. PaddleOCR handles numeric metadata (house numbers, ages, the deleted mark) and is `lang="en"` only, with the angle classifier **off**.

Both engines read at **200 DPI**. An earlier 300 DPI metadata pass was removed: the deleted mark is a thin enclosing loop, and at 300 DPI two of the five known-deleted cards dropped to 3/8 reads and were lost by any majority bar. 200 DPI is better for both engines.

**Card geometry**: Each voter page has a 3×10 grid — `_voter_card_rects()` returns **30 positions per page unconditionally**, whether or not the cells are filled. It is a lattice, not a count of populated cards. Partial tail pages (page 19 emits 14 records, page 21 emits 7) and blank pages simply return no record for the surplus slots; `_extract_card` returns `None` and the page loop skips it. A 22-page roll yields 660 grid positions and 531 records.

**Full-page rendering**: One 200 DPI render per page, then card crops are sliced from the NumPy array. This replaced ~30 per-card PDF rasterizations.

**Concurrency**: `ThreadPoolExecutor` over cards with `OCR_CARD_WORKERS` (default `min(8, cpu_count)`). Tesseract runs as subprocesses that overlap freely. The Paddle pass is **sequential by construction** — it runs on the main thread after the pool drains, so its cost is wall-clock and is not divided by the worker count. `_PADDLE_LOCK` guards *singleton construction only* (around line 70); it does not serialise `.ocr()` calls. Whole-request serialisation is an `asyncio.Lock`, `_OCR_LOCK`.

**Field extraction pipeline**: Tesseract text → `parse_voter_box_from_ocr_lines()` (label-based Hindi parsing) → focused re-reads for house number → a single **stacked** PaddleOCR pass (serial + house + age cropped, scaled, `np.vstack`'d, and routed back by mean-y of the result polygons) → multi-engine arbitration → the deleted-mark vote.

There is **no focused re-read pass for names or relations**. `RELATION_REGION` is defined and unused — relation names come from the same Hindi-block read as everything else.

**Per-card cost** (measured): Tesseract ~21–25 subprocess calls, PaddleOCR 9 (1 stacked + 8 deleted-vote readers).

## The Deleted Mark

This is the one field with a dedicated voting mechanism, so it is worth stating precisely.

**The design**: the `Q` is a *signal*, not the answer. A single read is not trusted — the crop is read 8 ways (4 pixel heights × {plain, Otsu}) and the mark counts as present only if **5 of 8** readers find it. If the vote lands in the inconclusive band (`0 < hits < 5`), a **second engine** is consulted: Tesseract, with the rule that the serial box holds a number, so **any alphabetic character in it is a Q candidate**.

**The two engines fail on disjoint cards and neither works alone.** On the ground truth, PaddleOCR reads the Q on all five known deletions; Tesseract reads *nothing at all* on page 15 card 30. Hence OR, not override.

**Preprocessing order matters more than the threshold.** Threshold **after** the upscale, never before. `resize(threshold(x))` interpolates across the threshold and returns a blurred, grey-edged binary — and a blurred threshold erases the thin loop of a Q. Measured on the same crop with the same 8 readers: page 16 cards 11 and 20 scored 4/8 with Otsu-then-upscale and 8/8 with upscale-then-Otsu. This was the actual cause of two missed deletions; the threshold was never the problem.

**What the vote costs**: ~270 ms/card, ~2.1 min over a 531-record roll, to detect 5 deletions — 8 of the pipeline's 9 Paddle calls per card, spent on the rarest field. It is the single most expensive OCR operation in the pipeline by a wide margin.

## Arbitration Rules

House-number arbitration is **inline** in `_extract_card` and the sequential Paddle pass — there are no `_choose_house_number` / `_choose_age` functions.

The rules are strict, and deliberately so: broad replacement caused 265 house-number regressions in a prior run.

- Paddle may only overwrite Tesseract when the Tesseract value contains **no Devanagari** (`if best_numeric and not has_devanagari`).
- A Devanagari Tesseract value **always** wins, even if Paddle's is longer.
- A slash number (`मकान संख्या`) requires **2+ agreeing reads**.
- A Paddle run may never be **shorter** than the Tesseract read it is merged with.
- A leading `1` is prepended only on an **exact** match (`paddle_house == "1" + cur_digits`).
- EPIC shape-invalid reads are fallback-only; a split vote **flags `_needs_review` but never rewrites the value**.

**Age is the exception.** Once Paddle's age crop is stacked — i.e. Tesseract's age was missing, non-digit, or under 18 — any in-range Paddle digit run **overwrites unconditionally**. There is no leading-`1` evidence rule for age; that documented rule does not exist in the code.

## Known Dead Code

Verified absent from the source; do not go looking for these:

- `_choose_house_number` / `_choose_age` — never existed as functions
- `serial_corrections` / `public_serial_corrections` — initialised, never appended to, so the count is always 0
- `RELATION_REGION` — defined, never referenced
- `/tmp/ocr_pdf_api.lock` — replaced by the `asyncio` lock
- `card_img` parameter of `_detect_deleted_watermark` — never referenced in the body

## Key Environment Variables

- `OCR_API_HOST` / `OCR_API_PORT` — server bind (default `0.0.0.0:8082`)
- `MAX_OCR_PDF_BYTES` — upload size limit (default 100 MB)
- `OCR_CARD_WORKERS` — concurrent card threads (default `min(8, cpu_count)`)
- `OCR_TRACE=1` — print every pipeline step to stdout
- `OCR_NAME_TOKEN_CORRECTIONS_JSON` — path to JSON for known OCR glyph corrections

## API Endpoints

- `GET /health` — status, engine info, busy flag
- `POST /ocr/extract` — main extraction (multipart: `pdf_file`, `start_page`, `end_page`, `whole_pdf`, `skip_non_voter_pages`)

There are no Celery batch endpoints in this file.

## Important Conventions

- Card geometry recalibration is required if the PDF template changes — always verify with boundary overlay scripts first.
- `is_deleted` is always a JSON boolean, never a string.
- `voter_sr_no` stays as a string (preserves OCR formatting). Public `sno` is an integer counter reset per request, unrelated to OCR.
- The surname field is named **`voter_sur_name`** everywhere — parser, `_empty_record`, gate, and output. There is no `voter_last_name`.
- Relation names split into first/middle/last component fields; full-name fields are not emitted.
- Roll metadata (`state_code`, `ac_code`, etc.) is extracted once from pages 1 and 3, then copied into every record.
- `photo` is not an output field. The photo region is only masked out of the card image before OCR.