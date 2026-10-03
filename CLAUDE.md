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

## Tesseract Line Grouping

`image_to_string` returns Tesseract's own lines. When `image_to_data` is used instead (it is, to carry per-word confidence) the lines must be **rebuilt**, and the grouping key is the composite `(block_num, par_num, line_num)` — not `line_num`.

Each of those three counters restarts at 1 inside its parent, and on a voter card **every printed row is its own paragraph**, so every row's first line carries `line_num == 1`:

```
blk par line  word   text
  1   1   1     1    'नाम'
  1   1   1     4    'मेता'
  1   2   1     1    'पिता'      <- paragraph 2, line 1 again
  1   2   1     4    'राजकुमार'
```

Grouping on `line_num` alone concatenated every such row into one line — `'नाम : सचिन मेता पिता का नाम: राजकुमार'` — which merged the voter's name into the relation field on **479 of 531** records. `word_num` is equally wrong in the other direction (it restarts per line, splitting every word). The three-part key reproduces `image_to_string`'s lines verbatim; `tests/test_tesseract_line_grouping.py` pins this, and pins the *source* text, so a revert fails loudly rather than passing while the pipeline breaks.

`_expand_line()` inside the parser still splits genuinely merged lines and is worth keeping as a safety net, but it is no longer the primary defence — the grouping key is.

## Serial Numbers

`voter_sr_no` comes from **one** Tesseract read of the serial box (voting over 3 scale targets), with no validation of the value against anything. There is no sequence check: `serial_corrections` is initialised and never appended to.

Measured on pages 3–8 (180 cards): **one** genuinely wrong read in 180 (page 6 card 26 returned `118` where `115` is correct). Everything else is exact.

**Do not validate serials by checking that they ascend.** They do ascend even when wrong — a systematic offset is perfectly monotonic, so a self-consistency check passes it. A bad read also shows up as an apparent duplicate or gap, which is how this one was found, but a shift that preserves ordering would slip through entirely. Align against ground truth or a known first serial, and align on a field that cannot collide (the EPIC), never on row position alone: `whole_pdf_6-8_results.json` carries one extra leading row (the tail card of page 5), so row *N* of ground truth is card *N+1* of page 6.

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

**The EPIC split vote, and when it flags.** Eight readers vote; the winner is the plurality, ties broken on length then insertion order. The chosen value is **never** changed by a disagreement — a split asks for a human check, it does not authorise substituting a different voter's ID.

A split only raises `_needs_review` when the readers were genuinely undecided: `EPIC_SPLIT_REVIEW_TOP_VOTES = 4`, so a 4-4 tie flags and 5-3 or better does not. Flagging every split flagged the mechanism *working* rather than a problem — 15 of 180 EPICs in pages 3–8 had a reader disagree, 12 of them on a single glyph (5 read as 8). On the seven splits ground truth covers, the majority was right **7 of 7** and the minority wrong **7 of 7**: one bad reader out of eight, not a close call. Resolved splits still go to `log.debug`. Do not lower this bar without new evidence that a majority can be wrong.

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