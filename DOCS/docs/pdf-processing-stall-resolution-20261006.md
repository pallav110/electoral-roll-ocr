# PDF processing stall: incident and resolution

## Status

Resolved and deployed on 6 October 2026.

The PDF pipeline is processing the remaining archive again. Completed PDFs and
their stored hashes were preserved. New ZIP objects in the MinIO
`electoral-archives` bucket continue to be checked every 30 seconds and will
enter the same discovery, deduplication, queue, OCR, and database workflow.

OCR speed tuning is deliberately outside this incident. This repair restores
correct scheduling, recovery, and forward progress without increasing the
configured unit concurrency.

## User-visible symptoms

- Hundreds of PDFs remained in `pending`, `retry`, or `failed` even though the
  OCR worker was mostly idle.
- Redis queue lengths could fall to zero while PDFs still had not run.
- The uploads page showed many documents as needing attention.
- The control worker retried dispatch every 15 seconds but made no forward
  progress.
- A prior restart/recovery left a large number of abandoned sessions.

## Original incident: Celery/Redis queue explosion

The first production problem was different from the stale-attempt recurrence
described below. The original architecture allowed the broker backlog to grow
far beyond the amount of real work.

### Original symptoms and measurements

- Approximately 4,000 genuine pending OCR units expanded to roughly 30,000
  Redis messages because the same queued/recoverable work was published more
  than once.
- Recovery could republish units that were already represented in Redis.
- OCR, document setup, discovery, recovery, and maintenance work shared the
  same default Celery queue.
- A single Celery consumer had to walk through duplicate/stale messages before
  valid OCR work reached the API.
- The UI measured a session from its early orchestration timestamp, so time
  spent waiting in Celery was presented as PDF processing time.
- One session displayed 8 hours 34 minutes even though its actual OCR execution
  was approximately six minutes; almost all of the displayed duration was
  artificial queue waiting.

Redis itself was healthy and the FastAPI/OCR services were responding. The
problem was how work was published, routed, recovered, and timed around them.

### Original failure model

```text
PostgreSQL pending units
        |
        | repeated/unbounded publication by dispatch and recovery
        v
Redis default Celery queue: genuine + duplicate + stale messages
        |
        | one shared consumer lane
        v
Control/maintenance work and OCR compete with each other
        |
        v
OCR API receives genuine work hours late
```

The practical analogy is a token counter with one cashier: there were about
4,000 customers, but duplicate tokens increased the pile to around 30,000. The
cashier and kitchen were functioning; valid orders simply reached the kitchen
very late.

### Plain Hinglish explanation

> OCR API slow ya faulty nahi thi. Actual problem Redis/Celery ki queue
> orchestration mein thi. Lagbhag 4,000 real OCR units ke duplicate aur stale
> messages baar-baar publish hokar kareeb 30,000 messages ban gaye. OCR,
> recovery aur maintenance ek hi lane mein chal rahe the, isliye genuine PDF
> ka task OCR API tak pahunchne se pehle ghanton wait karta tha. Redis kharab
> nahi tha; usko duplicate work diya ja raha tha. Paddle ko sequential rakhna
> intentional safety limitation hai. Queue split, bounded publishing aur safe
> recovery ne artificial backlog ko resolve kiya.

### Original fixes

- Split Celery routing into dedicated `control`, `ingest`, and `ocr` queues.
- Removed the legacy/default `celery` queue from normal task routing.
- Added bounded document and OCR-unit in-flight windows so Redis carries only a
  small delivery window instead of the entire database backlog.
- Made PostgreSQL the authoritative state and Redis a short-lived transport,
  preventing queue length from becoming workflow truth.
- Tightened recovery so work already queued or processing is not blindly
  republished.
- Reclaim queued rows only when reconciliation proves their broker message is
  absent and workers have been stopped safely.
- Skip and clean up units belonging to abandoned or non-current sessions so
  stale work cannot occupy the active window.
- Kept document setup and recovery on the control worker so long-running OCR
  cannot starve maintenance tasks.
- Corrected UI/session duration handling so queue wait is not reported as OCR
  execution time, including a duration backfill for existing sessions.

These fixes removed the original duplicate-message backlog and shared-lane
starvation. They were retained in the current deployment and verified with the
legacy queue at depth zero.

## PaddleOCR sequential-access constraint

PaddleOCR uses protected, sequential access inside each OCR service instance.
The global Paddle lock is intentional because concurrent access to the same
Paddle runtime can make it unstable or cause it to fail. This lock must not be
removed merely to increase Celery concurrency.

The important distinction is:

- **Intentional capacity constraint:** Paddle work is sequential within one OCR
  instance for safety.
- **Original bug:** duplicate/unbounded messages and unrelated tasks sharing one
  Celery queue caused hours of artificial waiting before OCR began.
- **Later bug:** stale document attempt counters caused the complete dispatch
  transaction to roll back before OCR work was published.

FastAPI and the OCR API were working as designed in both incidents. Queue
orchestration and persisted workflow state were the failures.

### Historical throughput estimate

At the time of the original diagnosis:

- Measured OCR throughput was approximately 11.94 seconds per page.
- Approximately 430 PDFs / 11,813 pages were estimated to require 39–42 hours
  after queue repair on one machine.
- Fixing, testing, rebuilding, and reconciling the original queue was estimated
  at one to two hours, for an overall estimate of roughly 40–44 hours.
- Processing 500 similar PDFs inside 12 hours would require about three seconds
  per page, around a four-times improvement over the measured single-instance
  rate.
- Paddle itself used approximately 1.6 seconds per page in the sampled path;
  Tesseract/card extraction consumed most of the remaining 9–10 seconds.

Consequently, raising Celery concurrency or enabling GPU for Paddle alone could
not safely promise 500 PDFs in 12 hours. The dependable estimate was four to
five independent OCR machines/instances, while preserving sequential Paddle
access inside each instance. These are historical planning estimates, not a
guarantee; current throughput must be benchmarked separately after correctness
has remained stable.

## Later recurrence: stale attempt history

`documents.attempt_count` is a denormalized counter. For 37 retry documents it
was lower than the highest attempt already stored in `extraction_sessions`.
One live example had `attempt_count = 0` even though attempts 1 through 3
already existed.

The dispatcher trusted the stale counter, incremented it, and attempted to
create attempt 1 again. PostgreSQL correctly rejected that row because
`(document_id, attempt_number)` is unique. Because document dispatch happened
inside one transaction, that single constraint error rolled back the whole
dispatch batch. The next scheduled run selected the same document and repeated
the failure, preventing all later PDFs from reaching OCR.

This was a control-plane/database consistency failure, not an OCR API latency
failure. OCR had no valid unit to consume after the failed dispatch transaction.

## Later recurrence as seen through Celery/Redis

### What appeared to be wrong

From the outside, this looked like a Redis or Celery queue failure:

- Hundreds of PDFs were waiting in PostgreSQL.
- The OCR worker was idle or processed only occasional units.
- Redis queue lengths often showed zero.
- Celery beat continued invoking the dispatcher, but pending work did not enter
  the OCR queue.

Redis was not losing PDF jobs. The control task reached Celery correctly, but
the dispatcher failed before it could commit valid sessions and publish their
work units. Redis therefore had nothing durable to deliver to the OCR worker.
An empty Redis queue meant "nothing was successfully published," not "all PDFs
are finished."

### Failure sequence

1. Celery beat placed `dispatch_pending_documents` on the `control` queue.
2. `worker-control` selected a retry document whose `attempt_count` was stale.
3. The dispatcher tried to insert an extraction session using an attempt number
   already present in PostgreSQL.
4. PostgreSQL raised a unique-constraint violation.
5. The complete dispatch transaction rolled back, including later documents in
   that batch.
6. No valid extraction units were published to the `ocr` queue.
7. Fifteen seconds later Celery beat repeated the same task, selected the same
   blocking document, and repeated the rollback.

In short: Celery scheduling worked, Redis delivery worked, and the OCR service
was available. The bridge from database work state to Celery messages was
blocked by invalid session numbering.

### Queue architecture retained by the fix

- `control`: document dispatch, unit dispatch, and recovery tasks.
- `ingest`: MinIO ZIP polling and archive ingestion.
- `ocr`: extraction-unit work consumed by the OCR worker.
- `celery`: legacy/default queue; it must remain unused and at depth zero.

The split prevents long OCR work from blocking control or archive-ingestion
tasks. PostgreSQL remains the source of truth; Redis is only the delivery
broker. Queue rows are kept in a bounded window so Redis is not filled with
hundreds of stale messages.

### Redis/Celery safeguards verified

- Queue routing sends tasks only to `control`, `ingest`, or `ocr`.
- The legacy `celery` queue remained at depth zero throughout verification.
- OCR delivery remained bounded at one processing unit and at most one queued
  unit under the current correctness-first configuration.
- Recovery ignores or removes orphaned units belonging to abandoned/non-current
  sessions, so they do not consume the in-flight window.
- The dispatcher now reconciles attempt history before publishing work, so a
  stale counter cannot roll back the batch repeatedly.
- MinIO polling continues independently every 30 seconds on `ingest` even while
  OCR is busy.

### Quick operational diagnosis

When PostgreSQL shows pending PDFs but Redis shows no OCR work, check the
control worker before blaming Redis:

```bash
docker compose logs --since=10m worker-control
docker compose exec redis sh -lc \
  'for q in control ingest ocr celery; do printf "%s=" "$q"; redis-cli LLEN "$q"; done'
```

Healthy behavior is:

- no repeating `UniqueViolation` or `IntegrityError`;
- `celery=0`;
- `ocr` stays near the configured bounded window rather than growing without
  limit; and
- completed units/pages continue increasing in PostgreSQL.

## False `WORKER_LOST` failures while waiting for sequential OCR

### Symptom

After the stale-attempt fix restored processing, the upload later showed 16
PDFs under **Needs attention**. All 16 changed at the same recovery tick and had
the same database state:

```text
status: failed
attempt_count: 6
max_attempts: 6
last_error_code: WORKER_LOST
last_error: Session heartbeat expired
pages_processed: 0
records_extracted: 0
```

These were not corrupt PDFs and the OCR API had not rejected them.

### Cause

The document window intentionally admits up to 20 PDFs while the correctness-
first OCR window runs one unit and holds at most one additional queued unit.
Paddle remains sequential. Sessions later in the 20-document window can
therefore retain only `pending` units for longer than the ten-minute document
stale threshold.

A recently added recovery branch treated a stale session with no
`processing`/`completed` unit as a dead worker. It failed to distinguish:

- a broken session with no unit rows at all; from
- a healthy session whose existing `pending`/`queued` units were waiting for
  their turn in the bounded OCR lane.

Recovery abandoned the healthy waiting sessions, retried them without doing
OCR, and repeated until their document attempts were exhausted. That created 16
false failures at the same timestamp.

### Fix

- Changed recovery to abandon this intermediate session state only when **no
  extraction-unit rows exist at all**.
- Any `pending`, `queued`, `processing`, or `completed` unit now proves the
  session has real work and must be left to unit dispatch/recovery.
- Added a regression test proving a stale session with pending units remains
  `processing`.
- Added a companion test proving a stale session with a page count but no unit
  rows is still recovered.
- Restored the 16 false `WORKER_LOST` documents to `pending`, cleared their
  synthetic errors/locks, and granted fresh recovery attempts without changing
  completed PDFs or duplicate hashes.

A second pre-repair PostgreSQL backup is stored at:

```text
/tmp/electoral-pre-worker-lost-repair-20261006.dump
```

### Live verification

- Expanded focused suite: **41 passed**.
- A scheduled recovery tick completed without errors.
- That tick observed 15 sessions older than ten minutes with pending/queued
  units and correctly preserved all 15.
- Failed documents after repair: **0**.
- Retry documents after repair: **0**.
- The upload returned to 211 completed, 20 processing, 336 waiting, and 15
  hash-detected duplicates immediately after recovery.

## Configuration audit

The supplied `.env` values did not cause the false `WORKER_LOST` failures. The
bug was the recovery predicate described above.

Two `.env` entries are overridden by the live Compose services:

| Setting | `.env` text | Effective live value | Result |
|---|---:|---:|---|
| `EXTRACTOR_TIMEOUT_SECONDS` | 120 | 5400 | A long OCR call is not killed at the two-minute boundary. |
| `PAGES_PER_UNIT` | 10 | 3 | Work is split into the three-page units observed in the UI/logs. |

Recovery-related effective values are:

| Setting | Effective value | Purpose |
|---|---:|---|
| `UNIT_STALE_SECONDS` | 600 | Reap only a unit already marked `processing` whose heartbeat stopped. |
| `DOCUMENT_STALE_SECONDS` | 600 | Detect a lost document setup/intermediate state; no longer applied to healthy waiting units. |
| `DOCUMENT_QUEUED_RECOVERY_SECONDS` | 120 | Republish document setup only when its broker delivery was lost. |
| `OCR_MAX_INFLIGHT_UNITS` | 2 | One executing unit plus one queued unit. |
| `DOCUMENT_MAX_INFLIGHT` | 20 | Bound the number of active document sessions. |
| `QUEUED_RECOVERY_SECONDS` | 10800 | Allow a legitimately queued OCR unit to wait behind a long extraction. |
| `CELERY_VISIBILITY_TIMEOUT` | 14400 | Prevent Redis from redelivering a still-running long OCR task. |

Other relevant observations:

- `MAX_UPLOAD_FILES=500` applies to the direct browser-upload guard; it did not
  block the 582-PDF MinIO ZIP, as the archive completed ingestion successfully.
- The current ZIP was about 3,362.2 MB and expanded to about 3,611.6 MB, within
  `MINIO_MAX_EXTRACTED_MB=5000`.
- `MINIO_MAX_ARCHIVE_MB=99999999` did not cause this incident, but it is
  effectively unbounded and weakens protection against unexpectedly large
  archives. It should be reduced later to a deliberate operational ceiling.
- `OCR_CARD_WORKERS=14` controls CPU card work; it does not remove the
  intentional sequential Paddle lock.

## Contributing conditions

- Earlier worker-loss and stale-session recovery paths had abandoned old
  sessions and returned documents to a retryable state.
- The persisted attempt history and document counter were not reconciled before
  a new session number was allocated.
- A dependency merge left both `PyMuPDF==1.24.10` and `PyMuPDF==1.24.14` in
  `requirements.txt`, which initially prevented the corrected application image
  from rebuilding.
- Historical abandoned units could interfere with in-flight accounting. The
  incoming orphan-unit cleanup fix was retained and verified.

## Code changes

### `app/workflow.py`

- Before allocating a new extraction session, query the maximum historical
  `ExtractionSession.attempt_number` for that document.
- Treat session history as authoritative when it is ahead of
  `Document.attempt_count`.
- Repair the stale counter in the same transaction, then allocate the next
  unused attempt number.
- Emit a warning when an automatic counter repair occurs so a future mismatch
  is visible without stopping dispatch.
- Correct stale-session recovery so a session is not abandoned merely because
  all of its units are still pending/queued behind sequential OCR.
- Preserve recovery for the genuinely broken state where `pages_total` exists
  but the session has no extraction-unit rows.

This makes dispatch self-healing and prevents one stale document from rolling
back every other document in the batch.

### `tests/test_queue_windows.py`

- Added a regression test that creates a retry document with
  `attempt_count = 0` and an existing abandoned attempt 1.
- Verified that dispatch creates attempt 2, publishes the document, and leaves
  the stored counter at 2.

### `requirements.txt`

- Removed the obsolete `PyMuPDF==1.24.10` pin.
- Kept the current `PyMuPDF==1.24.14` pin so Docker dependency resolution is
  deterministic.

## Live recovery procedure

1. Stopped `beat`, `worker-control`, `worker-ingest`, and `worker-ocr`.
2. Confirmed Redis depths were zero for `control`, `ingest`, `ocr`, and the
   legacy `celery` queue, and confirmed no extraction unit was active.
3. Created a PostgreSQL custom-format backup at
   `/tmp/electoral-pre-recovery-20261006.dump` (45 MB).
4. Reconciled each unfinished document's counter to the maximum attempt in its
   immutable session history.
5. Returned only approved documents in `pending`, `retry`, or `failed` to a
   clean `pending` state, cleared stale locks/errors, and granted three fresh
   recovery attempts.
6. Left all `completed` and hash-deduplicated `cancelled` documents unchanged.
7. Rebuilt and recreated the application, control, ingest, OCR-worker, and beat
   services.

Recovery affected 363 unfinished PDFs. It preserved 222 PDFs already completed
at the time of recovery and 15 PDFs intentionally cancelled as duplicate
content.

## Verification evidence

### Automated tests

The queue, recovery, upload-batch, and routing regression suite passed in the
rebuilt Compose image:

```text
41 passed
```

### Live end-to-end PDF

The first recovered PDF completed without manual intervention:

```text
File: 2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-543-WI.pdf
Attempt: 4
Pages: 29 / 29
Records: 830
Started: 2026-10-06 16:10:58 IST
Finished: 2026-10-06 16:16:09 IST
Elapsed: 311 seconds
```

The global completed count increased from 222 to 223, and the scheduler
automatically admitted the next pending PDF.

### Queue and database invariants

- Stale document attempt counters: **0**
- Active units belonging to a non-current session: **0**
- Legacy `celery` queue depth: **0**
- OCR flow remained bounded at one processing unit plus at most one queued unit.
- No `UniqueViolation`, `IntegrityError`, or worker error appeared after deploy.
- Database timestamps were observed with the `+05:30` Asia/Kolkata offset.

### Future ZIP ingestion

- Celery beat sent `ingest-minio-archives` every 30 seconds.
- The dedicated ingest worker received and completed every observed poll.
- The existing `BoothWise Voter After SIR 2026.zip` ingestion remains
  `completed` with 582 discovered PDFs.
- Polls correctly returned zero when no unseen ZIP version was present.

MinIO ingestion is idempotent by bucket, object key, and object ETag. Document
deduplication also retains the existing source identity and content-hash checks,
so a previously completed PDF is not reprocessed merely because the service is
restarted or the same archive is seen again.

## Operational result

The incident is resolved: PostgreSQL is again the authoritative workflow state,
Redis contains only short-lived delivery messages, dispatch cannot reuse an
existing session attempt number, and the OCR worker is receiving real units.
The remaining PDFs should continue moving from `pending` to `processing` to
`completed`; genuine extraction failures will still be surfaced rather than
hidden.

## Follow-up work (not part of this repair)

- Measure and tune OCR throughput/concurrency separately after correctness is
  stable.
- Run performance experiments against representative PDFs before changing the
  bounded unit window or Paddle worker settings.
- Retain the pre-recovery backup until the recovered archive has completed and
  the output has been spot-checked.
