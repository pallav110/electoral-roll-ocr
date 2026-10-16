# Real ECI downloader — status and findings

Notes from driving `scripts/real_download.py` against the live
`voters.eci.gov.in/download-eroll`. Everything here was measured, not assumed.

## The site is intermittently unavailable

`GET /download-eroll` returns **HTTP 503 with a completely empty body** under
load, including from a single browser — reproduced 90 seconds after a successful
load, so it is not a simple rate limit and does not back off reliably.

That matters because an empty 503 page has **zero `<select>` elements**. A run
against it reports every state as "Could not find value for state" and exits
having downloaded nothing, which is indistinguishable from a code bug.

Handled by `_open_form()`, which retries up to `PAGE_LOAD_ATTEMPTS` and waits for
`#stateCode option[value]` rather than trusting the HTTP status alone. Before
this, a 5xx was being reported as "captcha rejected" — the previous run's five
apparent captcha failures were almost certainly the outage, not bad codes.

## Bugs found and fixed

| Defect | Effect | Fix |
|---|---|---|
| `new_context()` without `accept_downloads` | Playwright silently drops the site's ZIP instead of firing a download event — looks exactly like a captcha rejection | `accept_downloads=True` on the context |
| `headless=False` | Aborts on a headless server before the site is touched | `HEADLESS` constant, `ECI_HEADLESS=false` to watch |
| Per-page 10-part allowance | Budget recomputed per page, so it tried to check 10 on *every* page — 13 spurious "site refused" messages in one run | Budget shared across pages, spent once |
| Captcha image never refreshed | Retry loop re-solved a **byte-identical** image and returned the same code every attempt; all 5 attempts were one guess | `_refresh_captcha_image()` re-triggers the form's captcha render and verifies the bytes actually changed |
| `_normalize_captcha` stripped non-alphanumerics | `"The code is: **aB3x9**"` became `ThecodeisaB3x9` — submitted as a real captcha, burning every attempt | Mines the labelled/trailing captcha-shaped token; rejects prose |
| 60 s download timeout | Slow-but-successful submits retried as failures | `DOWNLOAD_TIMEOUT_MS = 120000` |

`_normalize_captcha` and the part budget are covered by
`tests/test_part_budget.py`, which needs no network:

```
.venv-test/bin/python tests/test_part_budget.py
```

## Verified working

- **Captcha solving, live.** A real ECI captcha image was fetched and the vision
  model read it correctly as `ybw5tz`. The endpoint is
  `https://integrate.api.nvidia.com/v1/chat/completions` with model
  `meta/llama-3.2-11b-vision-instruct` — note **no `nvidia/` prefix**; sending
  one returns 404. The host must be `integrate.api.nvidia.com`:
  `api.nvidia.com` presents a certificate issued for `api.nvcf.nvidia.com`, so
  TLS verification fails before any captcha is attempted.
- **State dropdown**, 37 options, e.g. `NCT OF Delhi` → `U05`.

## Dropdown timing (the quiet failure)

A probe that selected the roll type by **index** (`select_option("#roleType",
index=1)`) reported a Central district containing `1 - NERELA … 5 - BADLI` —
none of which appear in `discovered_structure.json`, which records
`23 - KAROL BAGH`, `24 - PATEL NAGAR`. With no valid constituency selected the
site rendered **no parts table, an empty `#langCd`, and no captcha image at
all** — the whole form quietly degrades rather than erroring.

A follow-up probe walked all four 2025 roll types against Central and found
`23 - KAROL BAGH` present in **every one of them**, so the structure file is
current and the roll type is not the problem. The mismatched list was the
index-select racing the year dropdown's AJAX refill, i.e. a probe artifact.

The production path never made that mistake — it matches the roll type by label
— but the race is real, so `download_all_parts_for_combination` now waits for
`#roleType option[value]` to repopulate after the year is chosen instead of
trusting `networkidle`.

Consequence worth keeping: **a missing captcha image means the form is not
ready**, not that the selector is wrong. Both `probe_submit.py` and the
downloader's error paths now say so.

## The 10-part cap

Confirmed on-site: selecting an 11th part raises **"Maximum 10 Parts are allowd
once at a time"** and the checkbox never settles, so the click raises rather than
failing quietly. A constituency has roughly 170 parts across ~17 pages, so one
download covers at most the first 10; the remainder need further submissions.
`_check_parts_on_current_page` stops at the cap instead of raising, because
whatever is already checked is still a valid selection.

## Fake data in the bucket

`demo_upload_loop.py` uploaded 21 archives built from one local sample PDF. The
pipeline ingests them like real data. `scripts/purge_demo_archives.py` classifies
by **content hash**, not name:

```
python scripts/purge_demo_archives.py           # dry run
python scripts/purge_demo_archives.py --delete  # remove them
```

Current dry run: **21 simulated, 1 genuine** — `BoothWise Voter After SIR
2026.zip` (3.3 GB, 582 distinct parts) is correctly identified as real and left
alone. Deletion needs `--delete` and has not been run.

## Open

- The full download → ZIP → MinIO path has still not completed end to end.
- `discovered_structure.json` covers Delhi only.
- `create_organized_zips()` rewrites the master archive every batch and nests
  the constituency ZIPs inside it, duplicating every part.
- Sanitisation yields `Final_Roll___2025` (a space and a hyphen both become
  `_`), where the pipeline docs describe `Final_Roll_-_2025`.
