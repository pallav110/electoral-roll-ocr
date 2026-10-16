# Baseline notes — pages 6-8, aiml_improvements

Captured 2026-10-07 on the spxlpt133 laptop, OCR service on 127.0.0.1:8082.

## Environment correction

The running image was built 2026-10-05; `Dockerfile.ocr` last changed 2026-10-06.
The stale image carried `paddlepaddle-gpu` instead of the pinned CPU
`paddlepaddle==3.0.0`, so `import paddleocr` failed on `libcuda.so.1` and the
service silently reported `paddleocr_available: false` — house numbers and ages
empty, exactly as `ocr_api_requirements.txt:37-40` warns. Fixed by installing
the CPU wheel; `/health` now reports `tesseract+paddleocr`. Any baseline taken
before that fix would not match server behaviour.

## Machine is not the benchmark target

This is an **8-core** i7-8665U with 15 GB. The plan's 10.7 s/page baseline was
measured on the 16-core server. Here, pages 6-8 run at roughly **55 s/page** —
about 5x slower.

The cause is measurable, not mysterious. With `OCR_CARD_WORKERS=14` on 8 cores:

    load average: 14.97
    container CPU: 108%      <- ~1 core busy out of 8
    live tesseract procs: 14

All 14 workers fork a tesseract subprocess that deserialises a 4.1 MB
`eng.traineddata` per call. Concurrent bursts contend for disk and memory
bandwidth, so throughput *drops*. This matches the earlier microbenchmark
(8 workers 52.7 ms/call vs 14 workers 63.7 ms — 14 is worse).

`OCR_CARD_WORKERS=14` in `docker-compose.yml:94` was sized for a 16-core host
("12 of the 16 cores the WSL2 VM exposes"). It is over-subscribed here and
should be lowered for local benchmarking, or the local numbers will mislead.

## What this means for the plan

The speed target (500 PDFs in 24h) must be evaluated **on the server**, where
OCR_CARD_WORKERS=14 is correctly sized. Local runs are still valid for
*correctness* A/B and for counting launches, just not for throughput claims.

The per-launch deserialization cost this exposes is precisely what the
persistent tesserocr engines remove, so the case for Phase 2 is strengthened
rather than weakened — it is also what makes Phase 1's pool hoist necessary,
since warm engines only pay off if the thread outlives the page.

## Captured baseline — pages 6-8, 90 records, DETERMINISTIC

Two runs of the same build produced **0 differing fields**, so a later diff is
meaningful. `records_count: 90`, `needs_review: 0`, 0 pages skipped.

    run 1: 183.3s      run 2: 154.1s      (~55-61 s/page here)

### Launch profile, first capture

    grid_probe_cards     3
    header_state_code    1
    header_number        2
    header_anubhag       1
    hindi_text         540     <- dominant
    house_raw          337
    line_variants      183     <- WRONG, see below
                        ---
                        1067

**The `line_variants` count was wrong** — it sat outside the variant loop, so
it counted *invocations* rather than launches. Each invocation runs
`len(targets) x 2` launches (serial: 3x2=6, EPIC: 4x2=8). Corrected total is
about **2,166**, not 1,067. Fixed in the same commit; a re-capture is required
to get a trustworthy number.

### What the corrected profile changes about the plan

- **Hindi is the single biggest cost**, at 540 launches — 6 per card, two
  variants each pass. It was not previously identified as a target because the
  counters did not exist. It is a prime candidate for a persistent engine.
- **`house_raw` at 337** confirms the duplicate house read (Phase 4) is worth
  ~90-150 launches, not a rounding error.
- **The grid probe cost only 3** on these pages, not the ~96/page worst case,
  because every page here is a genuine voter grid and returns on the first hit.
  Phase 3 stays worthwhile for PDFs with cover pages, but is not the win on
  voter-dense input. It should not be prioritised ahead of Hindi.

## Phase 1 result (pool hoist) — correctness PASS, no speed claim

`compare_to_baseline.py phase1`: 90/90 records, **0 differing fields**.
Container file confirmed byte-identical to the host (md5) before the run.

    wall: 149.2s   vs baseline 183.3s (run 1) / 154.1s (run 2)

**That is not evidence of a speedup.** The two baseline runs differ from each
other by 29s on identical code, so 149s sits inside the run-to-run noise. This
is also expected: with pytesseract each call is a fresh subprocess, so keeping
the *thread* alive saves nothing. The hoist only pays off once a thread owns
something worth keeping, i.e. a persistent engine (Phase 2). Phase 1 is a
prerequisite, not a win by itself.

### Corrected launch total: 2,592 per 3 pages (~864/page)

The first capture's counters were incomplete. Sites added after it
(`house_whitelist`, `house_fallback`) and the `line_variants` fix account for
the jump from 1,067. Use this profile as the launch reference — Phase 1 does not
change launch counts, so it is valid for the baseline too:

    line_variants     1284   serial 6/card + EPIC 8/card
    hindi_text         540
    house_raw          337
    house_whitelist    238
    house_fallback     186
    grid_probe_cards     3
    header_*             4

My earlier "about 2,166" estimate was wrong for the same reason: it assumed the
uninstrumented sites were negligible. They were 424 launches. Also note that
**`line_variants` (serial + EPIC), not Hindi, is the largest cost** at 1,284 —
the earlier claim that Hindi was dominant came from the undercounted capture.
`deleted_crosscheck` and `deleted_watermark` read 0, so that path was not hit.

## Phase 2 result (persistent tesserocr engines) — PASS, ~3x speedup

Shadow mode validated end-to-end on real traffic first:
`OCR_TESS_BACKEND=shadow`, pages 6-8, two runs — **0 disagreements
sampled**, 2592 launches each, DETERMINISTIC vs itself. `tesseract_shadow_samples:
[]` (no mismatches recorded). TSV parse fix (prepend `_TSV_HEADER`
to `GetTSVText` rows, commit 1728f92) was a prerequisite: without it every
data-path read silently mismatched.

`OCR_TESS_BACKEND=tesserocr`, pages 6-8:

    wall: 54.8s     vs baseline run1 183.3s / run2 154.1s / Phase 1 149.2s
    ~18 s/page here vs ~55 s/page (pytesseract) on this 8-core laptop
    90 -> 90 records, 0 differing fields, needs_review 0 -> 0
    compare_to_baseline.py phase2: PASS - field-identical to baseline

Launch profile identical per-kind to pytesseract (2592) — engines are warm, so
the same 2592 reads now cost ~1 ms each instead of ~150 ms subprocess launch.
30 `engine_init` counts logged (one per worker-thread x lang build).
Container memory 1.7 GiB / 15.4 GiB — engine cache is cheap per thread.

`compare_to_baseline.py` prints the baseline launches from the stale
pre-counter-fix capture (1067); the correct reference is the 2592 profile
above — identical kind-for-kind, so Phase 2 changes no call counts, only
their cost.

## Environment blocker on this laptop

**GitHub is unreachable here** (`curl` to github.com exits 7, connection refused
-- not a slow link). `Dockerfile.ocr` builds leptonica 1.87.0 and Tesseract
5.5.3 from GitHub source tarballs, and falls back to apt when that fails --
but `libleptonica-dev` does not exist in the image's apt sources either, so the
build fails outright:

    E: Unable to locate package libleptonica-dev

Consequence: **the OCR image cannot be rebuilt on this machine.** That matters
because `docker compose up --force-recreate` reverts to the last built image,
discarding any fix applied inside a running container's writable layer -- which
is exactly how the CPU paddle install was lost once already.

Workaround in use: apply the paddle fix inside the running container
(`docker compose exec -T ocr pip install paddlepaddle==3.0.0`) and **do not**
force-recreate until the real rebuild happens.

The durable fix is committed in `Dockerfile.ocr` and
`ocr_api_requirements.txt` (uninstall any GPU wheel, then fail the build if
paddle cannot import). It needs a host that can reach GitHub.

Also worth knowing: the bind mount of `ocr_pdf_api.py` does **not** reliably
refresh on `docker compose restart` -- the container kept serving stale code
after several restarts and only picked up edits after `--force-recreate`.
Always confirm the running code matches the host file before trusting a test.

---

# Server results — 16-core host, aiml_improvements

All figures are for **pages 6-8 as a batch of 3**. Divide by 3 for per-page.
Production `8082` was never touched; every run used an isolated container on
`127.0.0.1:8084` with its own image tag.

## Throughput — 6.89 s/page, from 11.2

    Phase 2 run 1: 20.2s   6.73 s/page   0 field diffs   1.80 GiB
    Phase 2 run 2: 20.9s   6.96 s/page   0 field diffs

Against the production dashboard's 11.2 s/page that is **~1.6x**. Against the
laptop's pytesseract figure it is ~7-9x, but the laptop number is not a valid
comparison (see "Machine is not the benchmark target" above).

Capacity at 6.89 s/page: 86,400 / 6.89 ~= 12,540 pages/day. The reference PDF
averages ~28 pages (13,944 pages / 503 PDFs), so **~440-450 PDFs/day** against a
500 target. The dashboard shows 12.4 PDFs/hour, implying ~43% duty cycle, so
real end-to-end throughput is lower than the per-page rate implies and has not
been measured over a full run.

## Accuracy — 95.46% field level

`tests/test_scripts/compare_to_groundtruth.py`, pages 3,4,5,6,7,8,20,21
against the human-verified `ground_truth/json_files/`:

    5386 / 5642 fields correct = 95.46%     0 records missing

All 20 diffs are `ac_code` / `age` -- the same pattern as the pre-change
capture, so **no regression**, but not an improvement either.

Two bugs were found and fixed in the scoring script itself, both of which had
understated accuracy:

- Pages 20-21 records carry neither `epic_no` nor `id_card_no`, so `epic_key`
  fell through to `sno` and 23 blank records collapsed to a single key `'On'`.
  Adding `voter_sr_no` to the fallback chain fixed it.
- The script was diffing pipeline-computed metadata (`confidence`,
  `confidence_coverage`, `sno`, `anubhag_name`, `field_sources`) against
  ground truth that simply does not carry those keys.

Read 94.9% before the fix, 89.8% with the first bug only, 95.46% with both.

## Where the time goes — commit 20db954

    render                206.1 ms
    probe                 143.7 ms
    cards wall          3,947.7 ms     cards_cpu ~46,171 ms   12.0 @ 14 workers
    PADDLE PHASE       15,689.5 ms  <- 76% of wall, SERIAL, under _PADDLE_LOCK
    outside               ~683 ms
    total              20,670.0 ms   =  6.89 s/page

`cpu/wall = 12.0` at 14 workers is 85.7% efficiency -- **the card pool is
saturated**, so raising `OCR_CARD_WORKERS` above 14 will not help.

Bisection of the paddle phase (commit `092e38d`):

    deleted_mark_vote     10,586.0 ms   90 calls   117.6 ms mean
    paddle_ocr             5,910.6 ms   90 calls    65.7 ms mean
    crop_resize_stack         24.8 ms
    deferred_handoff_decode   10.4 ms
    result routing            ~3.0 ms
    postprocess                0.6 ms

`_decide_deleted(ser_img)` at `ocr_pdf_api.py:3944` is the single largest call
site -- 50% of wall clock at the time of that measurement.

**It is PaddleOCR, not Tesseract.** This was the key correction. Four direct
paddle reads per card -- four scaled/thresholded serial-box variants -- 360
calls, zero tesseract crosschecks (the tesseract path early-exits on these
pages). Consequences:

- The vote cannot be folded into `_card_pool` as-is; it inherits `_PADDLE_LOCK`.
- The earlier assumption that it was "Tesseract-based and therefore freely
  movable" was wrong and cost one measurement round.

Paddle accounting (commit `3f825b3`): 90 invocations, all per-card; **only
19/90 (21.1%) of results are used**; 65.0 ms mean, 82.1 ms p90; ~95 MiB
marginal RSS per instance. `OMP_THREAD_LIMIT=1` is set at
`ocr_pdf_api.py:45`, so all scaling must come from instance count, not intra-op
threads.

## The remaining lever, not taken

The paddle phase is one locked serial section: **5.23 s of every 6.89 s page**,
with 15 of 16 cores idle. Card OCR and paddle never overlap, so instances can
be added without contending for cores. Projected, at the card pool's measured
85.7% efficiency:

    K instances   page wall   s/page
    1 (today)     20,670      6.89
    2             14,132      4.71
    4              9,557      3.19
    6              8,131      2.71
    8              7,268      2.42

K=4 clears 5.4 s/page. Memory is not the constraint -- 8 instances is ~760 MiB
against 1.80 GiB in use and 23 GiB available.

**This was not attempted.** It is gated on whether concurrent PaddleOCR
instances are safe. The code carries the comment that PaddleOCR is *"not
thread-safe even with a lock"*, and that claim has never been re-verified under
concurrency -- it was flagged twice and both times deferred. To answer it:
construct K *separate* instances, run the real workload at K=2,4,6,8, gate on
zero field diffs plus two agreeing runs (nondeterminism is the risk, not
exceptions), and count empty/all-zero paddle results per card against the K=1
run. Silent corruption is the failure mode that matters here.

## Status

- Branches `aiml_improvements` (Phases 1, 2, 4 + instrumentation) and
  `aiml_backend` (deployed) are **not merged**. Production is running the old
  code and has gained nothing; the gains above are measured, not shipped.
- The DB-backed pytest suite has **not** been run since Phase 4. Field diffs
  against baseline passed, but pytest is a separate check:
  `docker compose run --rm -v "$PWD/tests:/code/tests:ro" web python -m pytest -q tests`
- `tests/test_scripts/server_phase2_check.sh` exists but was **never run on the
  server** -- it force-recreates the live 8082 container. Codex ran an
  equivalent isolated benchmark on 8084 instead. Do not run it against a
  production container with work in flight.