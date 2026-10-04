# Patch A — per-card progress endpoint for `ocr_pdf_api.py`

**Status: APPLIED and verified. 2026-10-05.**

The five edits described below are live in `ocr_pdf_api.py` and deployed.
Extraction output is unchanged — see "Verification" at the bottom.

## Why this exists

You asked for a counter that ticks up every 5 seconds showing cards being
scanned, rather than jumping to 90 after three pages. Option B (built first)
fixes this at *batch* granularity from the database. This patch fixes it at
*card* granularity, which is the only thing that can show a smooth counter at
all — the real session is 7 units over pages 3–22 at ~70 s each, so a batch
counter still has seven long flat stretches.

**The OCR service already knew how many cards it had done.** It wrote a
progress line per card to stdout and that output went nowhere. The information
existed and was discarded.

This adds a read-only in-memory counter and one `GET` endpoint. The `web`
service polls it alongside the database.

## What it does and does not do

**Does:**
- Count cards as they complete, in memory.
- Serve the count over `GET /ocr/progress`.
- Cost one integer increment per card.

### On "cards" vs "records" — read this before trusting the number

`_voter_card_rects()` returns **30 lattice positions per page unconditionally**,
whether or not the cell holds a voter card. The real session is 20 pages
(pages 3–22), so:

```
grid positions : 20 x 30 = 600
records        : 531
difference     :  69 blank lattice slots
```

Page 19 emits 14 records from 30 slots, page 21 emits 7, and **page 22 emits
none at all** — yet all three are 30 slots each.

So "cards scanned" and "voter records found" are different numbers, and the
endpoint reports both (`cards_done`, `cards_records`). They must never be shown
under one label. The UI shows `cards_records` and labels it "Voter records
scanned"; `cards_done` is exposed for the "N of M cards" sub-line.

**Does not:**
- Touch extraction. No record, no field, no arbitration, no threshold changes.
- Add a file, a database table, or a migration.
- Change timing. The increment is outside the OCR read path.

## The safety argument

The concern that governs this file is that a change here can silently alter
extraction output. This patch is separated from that path by construction:

1. **The counter is written in one place**, immediately before
   `extract_card_at` returns. It cannot influence what that function computed.
2. **The endpoint is a new route.** Adding a route does not change existing
   ones; `/ocr/extract` and `/health` are untouched.
3. **It is pure in-memory state**, reset at the start of each request. A stale
   count cannot leak into a subsequent run.

---

## What was actually applied

### Change 1 — the counter (`ocr_pdf_api.py` lines 173–248)

`_PROGRESS_SNAPSHOT` plus `_progress_reset`, `_progress_card_done`,
`_progress_page_done`, `_progress_finish`, `_progress_snapshot`, each guarded by
the existing `_PROGRESS_LOCK`. `_progress_snapshot` returns a `dict()` copy —
handing out the live dict would let a serialiser mutate the counter or let a
caller hold a reference across a reset and write into the next run.

Two derived fields, `elapsed_s` and `idle_s`, exist because the web service
needs to tell a *running* counter from a *leftover* one. `idle_s` is the gap
since the last card. See "The staleness trap" below.

### Change 2 — four call sites

| Line | Call | Where |
|---|---|---|
| 3203 | `_progress_reset(pages_total=max(0, last - first + 1))` | after `_resolve_page_range` |
| 3281 | `_progress_card_done(page_number=..., records=1 if rec else 0)` | in `extract_card_at`, before `return rec` |
| 3521 | `_progress_page_done(page_number, len(card_rects))` | after the page-complete print |
| 3547 | `_progress_finish()` | after the page loop drains |

### Change 3 — the endpoint (line 3623)

```python
@app.get("/ocr/progress")
async def ocr_progress() -> dict[str, Any]:
    snap = _progress_snapshot()
    return {**snap, "running": not snap["done"] and snap["updated_at"] > 0}
```

### Change 4 — the consumer (`app/`)

`fuse_progress()` in `app/presentation.py` merges the two clocks;
`ocr_progress_snapshot()` in `app/main.py` fetches the counter with a 2-second
timeout and never raises; `_fused()` supplies the running unit's page range.

---

## The staleness trap

`_OCR_LOCK` serialises whole requests, so between two units there is a gap with
no OCR work in it. In that gap `done` is `False` again (the next request reset
the counter) while `active_page` still names the **previous** unit's last page —
which is inside that previous unit's range. A page-range check alone would
therefore accept a leftover counter.

Requiring both `not done` **and** `idle_s <= 30` rejects all three cases: a
leftover counter, a between-units gap, and a counter never exercised at all
(which reads `done=False, idle_s=0.0, cards_done=0`).

## Why a page-range check is sound

The counter is process-global — it knows nothing about sessions. It is mapped
to a session by `active_page` falling inside the running unit's range. That is
only unambiguous because **the worker runs `--concurrency=1`** (CLAUDE.md: two
units at once put an 8-core box at load 40). With one worker there is exactly
one unit in flight in the whole system, so a page inside that range is this
session's work and nothing else.

If concurrency is ever raised, this needs a `job_id` threaded through the
extraction signature — which is a change to extraction, and so is out of scope
for a read-only patch. Flagged rather than hidden.

---

## Verification

**Extraction unchanged.** `tests/test_scripts/golden_diff_6_8.py`, pages 6–8:

```
run1 vs run2: identical
counter after run: cards_done=90 cards_records=90 pages_done=3 done=True
response records=90  (counter agrees)
patched vs ground truth: DIFFERS -- record 24 'voter_sr_no': '115' != '118'
```

That last line is exit code 1, and it is **not** a regression. A targeted
re-check of all 90 serials against ground truth found exactly one difference,
at page 6, which CLAUDE.md already documents:

> *"one genuinely wrong read in 180 (page 6 card 26 returned `118` where `115`
> is correct)"*

**Counter verified against a live extraction.** `watch_card_counter.py` runs a
real 3-page extraction while polling the real endpoint:

```
   time  live  cards  headline  page
      8s  True       3         3     3
     16s  True      30        30     3
     36s  True      60        60     4
     56s  True      87        87     5
     58s  True      90        90     5

OK: 15 distinct values from 3 to 90 -- the headline moves per card, not per batch.
OK: final counter (90) matches the response exactly.
```

15 distinct values where the previous display showed one.