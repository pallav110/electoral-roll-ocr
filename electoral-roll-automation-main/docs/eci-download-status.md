# ECI downloader — findings so far

## The blocker was a flaky JS bundle, not the captcha

`https://voters.eci.gov.in/static/js/main.<hash>.js` — the Angular bundle —
intermittently returns **503**.

Evidence (`scripts/isolate_bootstrap.py`, log in `isolate_log.md`):

```
503 script   /static/js/main.b35ed2f3.js
REQ FAILED GET .../main.b35ed2f3.js :: net::ERR_ABORTED
main document HTTP 200
rootChildren: 0     stateCode: "SELECT NOT IN DOM"     forms: 0
gateway calls: 0
```

Eight `curl`s to that exact URL immediately afterwards: HTTP 200 every time,
1,823,277 bytes each, ~0.25 s.

So the page loads fine (the 1548-byte shell is correct — it is a
client-rendered app with an empty `<div id="root">`), and the *bundle* is what
fails.

### Why this looked like four unrelated bugs

| Symptom | Actual cause |
|---|---|
| "Form load failed, 503" | Real, but the 503 was the bundle, not the page |
| "state not found" for all states | App never booted; the dropdown does not exist |
| Vision model returned the same code 5x | `_refresh_captcha_image` clicked submit inside a dead page, so the `<img>` src never changed |
| "Captcha rejected / 400" | Still unexplained — see open questions |

A page reload re-requests the same flaky bundle, so consecutive retries can all
lose the same coin flip. The original 5-attempt loop made this *worse*.

**Fix applied** to `src/eci_downloader.py`:
- `_open_form` distinguishes the two failure modes with `_app_booted()`
  (empty `#root` ⇒ bundle 503'd; booted but dropdown empty ⇒ the state XHR failed).
- Attempts 5 → 12.
- Readiness is judged on the DOM, not the HTTP status of the document.

## Open questions

Both are now closed — see below.

## The request contract (recovered from the bundle, not guessed)

The Angular chunk `6838.fe8807b3.chunk.js` builds the submit body verbatim:

```js
var e = {
  stateCd:          Sl?.stateCode,          // "U05"
  acNumber:         Sl?.constituency,       // "23"
  partNumberList:   ut,                      // part NUMBERS, not names
  districtCd:       Sl?.district ?? "",      // "U0505"
  captcha:          O,                       // the 6-char code
  captchaId:        et?.id,                  // from getCaptcha/EROLL
  langCd:           Sl?.langCd ?? "",        // "ENG"
  publishedRollId:  Sl?.roleType ?? "",      // "U05-2025-FIR"
  misKey:           "EROLLA32DVI09AJH"      // hardcoded client constant
};
```

Three things could not have been guessed:

- **`captchaId`** — `id` from `GET /captcha-service/getCaptcha/EROLL`. The code
  alone is meaningless to the server without it. This is the actual cause of the
  400s.
- **`misKey`** — a hardcoded constant, not a value read from the page.
- **`publishedRollId` / `stateCd` / `acNumber`** — sourced from the SPA's own
  state object, so a selector's *label* is the wrong thing to send; the *value*
  attribute is.

### Why the checkboxes carry no name/value/id

The SPA builds its table rows as:

```js
e.push({col1: t, col2: `${l.partNumber} - ${l.partName}`, col3: l.partNumber})
```

then submits `Object.values(ye[Ae[i]])[2]` — i.e. `col3`, the **part number**.
The checkbox stores only `+t.index` (a row index), and `ye` maps that index to
the row. The identifiers are never in the DOM, which is why reading the DOM could
never have produced a valid request.

### The download is two requests

`generate-published-pdfs` answers with ~104 bytes:

```json
{"statusCode":200,"message":"...","payload":"<path>"}
```

The SPA then does `fetch(base + "/eroll/" + path)` → blob → save. The old code
waited for a ZIP **in the generate response** and, having filtered for bodies
over 100 bytes, printed nothing — the one response that explained every failure
was the only one never shown. Base is module 48454's `U` = `https://voters.eci.gov.in`.

### Client-side guard clauses

`"" !== stateCode`, `"" !== constituency`, `"" !== langCd`, `"" !== roleType`,
`Me.length !== 0`, `"" !== captcha` — and `Me.length > 10` → *"Maximum 10 Parts
are allow once at a time"*. Every one of those silent-empty cases is a way our
automation could click submit and get a 400 instead of the real message.

## Vision model roster

`GET /v1/models` returns 80 entries for this key, but **listing ≠ serving**.
Probed against a live captcha:

| Model | Result |
|---|---|
| `meta/llama-3.2-11b-vision-instruct` | 200, correct 6-char read |
| `meta/llama-3.2-90b-vision-instruct` | times out past 90 s |
| `microsoft/phi-3-vision-128k-instruct` | in the catalogue, **404** |
| `microsoft/kosmos-2`, `adept/fuyu-8b`, `nvidia/cosmos-reason2-8b` | 404 |
| `google/gemma-3-27b-it` | 410 Gone |

Only one answers, so consensus is currently single-shot. `scripts/find_vision_models.py`
re-runs the probe; it exits non-zero below 2 working models for that reason.

## Honest status

- Captcha solving **works**: the vision model returns correct 6-char codes
  (`zgz76g`, `gay2fk`, `mps4yt`, …).
- Form fill + parts table **work** once the bundle loads.
- **Zero real PDFs downloaded.** `downloaded_pdfs/` is empty.
- The 21 archives in MinIO `electoral-archives` are **simulated**, from the demo
  loop. They are not real electoral rolls.
- There is **no working end-to-end demo**.

## Selector values that work

| Field | Selector | Value |
|---|---|---|
| state | `#stateCode` | `U05` |
| year | `#revyear` | `2025` |
| roll | `#roleType` | `U05-2025-FIR` (Final Roll - 2025) |
| district | `#district` | `U0505` (Central) |
| constituency | `#constituency` | `23` (KAROL BAGH) |
| language | `#langCd` | `ENG` |

Captcha is `data:image/jpg;base64,...` inline on `#captcha` (field `name=captcha`).
Site enforces **max 10 parts per submission**.

## Environment notes

- `~/.cache/ms-playwright` was missing on this machine and had to be reinstalled
  (`playwright install chromium`, 114 MB). Nothing on the box (no cron, no
  tmpfiles rule, 85% disk) targets it.
- With `channel="chrome"` (system Chrome) the form **never renders** even when
  the document returns 200. Playwright's bundled chromium works. Keep using it.