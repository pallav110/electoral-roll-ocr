# Improvements in Speed and Time

## Objective

Improve the electoral-roll OCR pipeline so that approximately **500 PDFs can be processed within 24 hours on one 16-core machine**, without weakening extraction accuracy or removing the protection that keeps PaddleOCR stable.

## Current measured performance

The live workload was measured at approximately:

- **10.7 seconds per page**
- **5–6 minutes of actual processing per average PDF**
- **10–11 completed PDFs per hour**
- **Approximately 240–260 PDFs per day**
- **Approximately 13.8 of 16 CPU cores in use during OCR**

For 500 similarly sized PDFs to finish in 24 hours, the pipeline must sustain:

- **20.8 PDFs per hour**
- Approximately **5.4 seconds per page**
- Nearly a **2x throughput improvement** over the measured baseline

These figures are estimates based on the current batch. PDFs with more pages, damaged scans, unusual layouts, or difficult text will take longer.

## What is actually slow

The earlier Redis/Celery defect caused artificial queue delays, duplicate task publication, and misleading multi-hour processing durations. That orchestration issue is separate from the OCR computation time.

After correcting queue management, the remaining performance bottleneck is primarily inside card-level OCR:

1. A PDF page is divided into voter-card images.
2. The cards are processed concurrently using the configured card-worker pool.
3. Several Tesseract variants are run for names, serial numbers, EPIC numbers, house numbers, and other fields.
4. Many calls start a new Tesseract subprocess.
5. PaddleOCR is intentionally serialized to protect its model and runtime.

Observed approximate page timing:

- PDF rendering: **0.06–0.08 seconds**
- Grid detection: **0.54–0.60 seconds**
- Sequential PaddleOCR work: **1.6–1.7 seconds**
- Total page processing: commonly **8.5–10.8 seconds**

PaddleOCR is therefore only part of the total time. Most remaining time is associated with repeated card-level Tesseract work and process-launch overhead.

## Changes that will not provide the required improvement

### Increasing `OCR_CARD_WORKERS` above 14

The OCR container already uses close to 14 CPU cores. Raising this value to 15 or 16 leaves insufficient capacity for the operating system, Docker, database, Celery, Redis, and web services. It may increase contention and make processing slower or unstable.

### Increasing Celery OCR concurrency

One OCR request already distributes card work across many CPU threads. Running multiple requests concurrently would cause the requests to compete for the same 16 cores. Celery concurrency should remain conservative unless the inner OCR parallelism is redesigned at the same time.

### Removing the Paddle lock

The sequential Paddle protection is intentional. Removing it can cause concurrent model access, GPU contention, memory problems, or crashes. Optimizations must preserve sequential Paddle execution.

### Publishing more Redis tasks

More queued tasks do not create more CPU capacity. An excessively large queue previously caused duplicate and stale work. Dispatch should remain bounded and idempotent.

### Increasing `PAGES_PER_UNIT`

Larger units can reduce a small amount of request overhead, but they do not remove the principal OCR cost. Very large units also increase retry cost and the time before progress is committed.

## Recommended code optimizations

### 1. Introduce persistent Tesseract engines

Current OCR helpers use `pytesseract`, which launches Tesseract processes repeatedly. With many cards and several OCR variants per card, one page can cause hundreds of process launches.

Recommended implementation:

- Evaluate `tesserocr` or another supported persistent Tesseract API.
- Create one isolated, thread-local engine per card worker.
- Explicitly reset language, page-segmentation mode, whitelist, and other variables between calls.
- Do not share a mutable Tesseract engine concurrently between threads.
- Keep a compatibility mode so the current implementation remains available during comparison testing.

Expected impact: this is the largest potential code-only improvement, but its actual benefit must be benchmarked.

Affected files:

- `ocr_pdf_api.py`: introduce a thread-local engine manager and replace repeated subprocess calls.
- `Dockerfile.ocr`: install the required native Tesseract development/runtime dependency and Python binding.
- `docker-compose.yml`: add a feature flag and optional tuning variables.
- `docker-compose.gpu.yml`: mirror relevant OCR configuration without hardcoded machine-specific worker counts.
- `docker-compose.linux.yml`: mirror relevant OCR configuration where applicable.
- `.env`: expose the selected Tesseract mode and worker configuration.

### 2. Add validated early exit for OCR variants

Serial and EPIC extraction can currently try multiple scales and threshold variants even when the first results already agree.

Recommended implementation:

- Run the two historically strongest variants first.
- Normalize both results.
- Stop early only when both agree and the value passes strict field validation.
- Run all remaining variants when results disagree, confidence is weak, or validation fails.
- Apply a similar fallback approach to Hindi text: execute the second image variant only when the first result is missing or scores poorly.

This retains the robust fallback for difficult scans while avoiding unnecessary OCR calls for clean cards.

Affected file:

- `ocr_pdf_api.py`: update the multi-variant readers and add counters showing how often fast-path and fallback-path processing are used.

### 3. Eliminate repeated house-number OCR

Parsed and raw house-number values should be returned by one extraction operation. The same crop should not be preprocessed and OCRed twice merely to obtain both representations.

Affected file:

- `ocr_pdf_api.py`: return a structured result containing both raw and normalized house-number values.

### 4. Cache document-level metadata

Header and document metadata that remain constant throughout a PDF should be extracted once and reused by later page units.

Recommended implementation:

- Store normalized metadata against the document or content hash.
- Reuse it for every unit of the same document.
- Invalidate the cache when the extraction schema version changes.

Affected files:

- `ocr_pdf_api.py`: avoid repeated header OCR when trusted metadata is supplied.
- Pipeline task/model code: persist and forward cached document metadata.
- Database migration, only if the existing document metadata fields are insufficient.

This is a smaller optimization than persistent Tesseract but is low risk when keyed correctly.

### 5. Pipeline CPU preparation and Paddle work carefully

Paddle must remain sequential, but independent preparation for the next page may be overlapped with Paddle processing for the current page.

Any implementation must:

- Keep exactly one active Paddle inference operation.
- Use bounded buffers rather than unbounded queues.
- Reserve CPU capacity for Paddle and system services.
- Avoid running two full 14-thread card pools simultaneously.
- Apply backpressure when Paddle cannot accept the next prepared batch.

Affected file:

- `ocr_pdf_api.py`: separate page processing into bounded preparation, card OCR, Paddle, and result-assembly stages.

This is expected to provide an incremental improvement rather than a complete 2x gain.

## Configuration strategy for different machines

Worker counts should not be hardcoded in Docker images. Dockerfiles should define software and dependencies; runtime sizing belongs in environment variables or Compose overrides.

Suggested configuration:

```env
OCR_CARD_WORKERS=14
```

An 8-core laptop could instead use:

```env
OCR_CARD_WORKERS=6
```

A safe automatic mode could calculate:

```text
max(1, available_cpu_count - 2)
```

The explicit environment variable should always override automatic detection. Automatic detection must respect container CPU limits rather than assuming every host CPU is available.

## Required instrumentation

Before changing the extraction algorithm, add per-stage measurements for:

- PDF rendering
- Grid detection and card cropping
- Hindi-name OCR
- Serial-number OCR
- EPIC OCR
- House-number OCR
- PaddleOCR wait time
- PaddleOCR execution time
- Result parsing and database write time
- Number of Tesseract calls per page
- Number of fast-path versus fallback-path calls
- Queue waiting time versus actual processing time

Metrics must distinguish:

- `queued_at`
- `processing_started_at`
- `processing_finished_at`
- actual OCR duration
- retry duration

This prevents queue waiting from again being presented as OCR processing time.

## Safe implementation and validation plan

The live queue should not be stopped merely to experiment with performance changes.

1. Add timing instrumentation without changing OCR results.
2. Select a representative benchmark set of 20–50 PDFs containing clean, difficult, and previously problematic scans.
3. Save the current output as the accuracy baseline.
4. Implement one optimization behind a feature flag.
5. Build a separate experimental OCR image or service.
6. Run the same benchmark set through both versions.
7. Compare every extracted field, including names, EPIC numbers, serial numbers, house numbers, ages, relationships, deleted flags, record counts, and failures.
8. Measure median and high-percentile page duration, not only the fastest result.
9. Promote an optimization only when it provides a repeatable improvement without unacceptable output regression.
10. Roll out gradually and retain the previous image for immediate rollback.

## Acceptance criteria

For the single-machine 500-PDF/day objective, the optimized system should demonstrate:

- Median throughput near or below **5.4 seconds per page** on a representative dataset.
- At least **20.8 similarly sized PDFs per hour** over a sustained test.
- No increase in failed documents or exhausted unit attempts.
- No duplicate voter records or duplicate OCR units.
- No Paddle concurrency crashes.
- No material extraction-accuracy regression against the baseline.
- Accurate separation of queue wait time and actual OCR time in the UI.

If the optimized system cannot meet these criteria, 500 PDFs/day cannot be promised reliably on this one machine. Additional independent OCR machines or instances would then be the dependable scaling method.

## Expected outcome

Small, low-risk changes may provide approximately **15–30% improvement**. Persistent Tesseract engines combined with validated early exits and removal of duplicate OCR operations are the only credible code-level path toward a **1.5–2x improvement** on the existing machine.

The 2x result is a performance target, not a guarantee. It must be proven using representative PDFs and field-by-field accuracy comparison before deploying to the production queue.
