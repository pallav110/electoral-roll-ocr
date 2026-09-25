# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Electoral-roll OCR pipeline that extracts structured voter records from Hindi PDF voter lists. Single-file FastAPI service (`scripts/ocr_pdf_api.py`, ~2600 lines) containing all extraction logic — no local imports.

## Commands

```bash
# Activate virtualenv
source .venv/bin/activate

# Start API server (default 0.0.0.0:8082)
python scripts/ocr_pdf_api.py

# Start Celery worker (requires RabbitMQ)
python scripts/ocr_pdf_api.py worker

# Run test/debug scripts
python tests/test_scripts/debug_first_three_ocr.py
python tests/test_scripts/test_whole_pdf_boundaries.py
```

## System Dependencies

Tesseract with Hindi language data must be installed: `tesseract-ocr` and `tesseract-ocr-hin`. Python deps in `ocr_api_requirements.txt`.

## Architecture

**Dual OCR engine design**: Tesseract handles Hindi+English text (voter names, relations, addresses) at 200 DPI. PaddleOCR handles numeric metadata (serial numbers, EPICs, house number fallbacks, DELETED watermark detection) at 300 DPI. These DPI levels are intentional — Hindi OCR is better at 200, metadata extraction needs 300.

**Card geometry**: Each voter page has 30 cards (3 columns × 10 rows). Row boundaries are explicitly measured ratios, not equal-height divisions — equal-height accumulates drift and shifts crops into whitespace. `_voter_card_rects()` is the single source of truth for all card positions.

**Full-page rendering**: One 300 DPI render per page, then card crops are sliced from the NumPy array. This replaced ~60 per-card PDF rasterizations.

**Concurrent card OCR**: `ThreadPoolExecutor` with configurable `CARD_WORKERS`. Tesseract runs as subprocesses that overlap freely. PaddleOCR is serialized through `_PADDLE_LOCK` (not thread-safe). Cross-process serialization uses a file lock at `/tmp/ocr_pdf_api.lock`.

**Field extraction pipeline**: Tesseract text → `parse_voter_box_from_ocr_lines()` (label-based Hindi parsing) → focused 200 DPI re-reads for names/relations → PaddleOCR metadata passes → multi-engine arbitration (`_choose_house_number`, `_choose_age`) → serial reconciliation (page-level majority vote).

**Arbitration rules are strict**: Paddle cannot overwrite a valid Tesseract house number just because it's longer. Focused relation names require surname agreement or missing primary. Age corrections require exact leading-`1` evidence. These rules exist because broad replacement caused 265 house number regressions in a prior run.

## Key Environment Variables

- `OCR_API_HOST` / `OCR_API_PORT` — server bind (default `0.0.0.0:8082`)
- `MAX_OCR_PDF_BYTES` — upload size limit (default 100 MB)
- `OCR_CARD_WORKERS` — concurrent card threads (default `min(8, cpu_count)`)
- `OCR_CELERY_BROKER_URL` — RabbitMQ broker for batch mode
- `OCR_BATCH_ALLOWED_ROOTS` — restrict server-side path batch access
- `OCR_NAME_TOKEN_CORRECTIONS_JSON` — path to JSON for known OCR glyph corrections

## API Endpoints

- `GET /health` — status, engine info, busy flag
- `POST /ocr/extract` — main extraction (multipart: `pdf_file`, `start_page`, `end_page`, `whole_pdf`, `skip_non_voter_pages`)
- `POST /ocr/batch/upload` / `POST /ocr/batch/path` — Celery batch jobs
- `GET /ocr/jobs/{job_id}` — batch job status

## Important Conventions

- Card geometry recalibration is required if the PDF template changes — always verify with boundary overlay scripts first.
- `is_deleted` is always a JSON boolean, never a string.
- `voter_sr_no` stays as a string (preserves OCR formatting). `sno` is an integer counter reset per request.
- Relation names split into first/middle/last component fields; full-name fields are not emitted.
- Roll metadata (`state_code`, `ac_code`, etc.) is extracted once from pages 1 and 3, then copied into every record.
