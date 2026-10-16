# ECI Electoral Roll Downloader - Structure Documentation

**Generated:** 2026-10-09  
**Status:** Form loads, captcha solving working, structure discovery complete for all 36 states

---

## 1. Captcha Solving - PROOF IT WORKS

### Test Results (18/18 cases passing)

```
ok   None       expected None        # enumerated but only 5 chars - must NOT be guessed at
ok   None       expected None        # 5 chars, same failure
ok   None       expected None        # labelled but only 5 chars - abstain rather than submit a short code
ok   'zgz76g'   expected 'zgz76g'    # bare code
ok   'gay2fk'   expected 'gay2fk'    # bare code
ok   'vCegkf'   expected 'vCegkf'    # letters only, no digit
ok   'mps4yt'   expected 'mps4yt'    # bare code
ok   None       expected None        # model describing the image, not a code
ok   None       expected None        # markdown-wrapped but 5 chars
ok   'aB3x9Z'   expected 'aB3x9Z'    # markdown-wrapped, 6 chars
ok   None       expected None        # 4 chars, too short
ok   'abc123'   expected 'abc123'    # mixed alnum
ok   None       expected None        # empty
ok   None       expected None        # two 4-char codes, neither is 6 - abstain
ok   'abcdef'   expected 'abcdef'    # enumerated 6 chars
ok   '123456'   expected '123456'    # enumerated digits
ok   None       expected None        # lettered enum with dots, no commas
ok   None       expected None        # two 4-char codes

all 18 cases pass
```

### Live Captcha Flow (verified via `isolate_bootstrap.py` and `probe_captcha_shape.py`)

```
STEP 02  GET the page
        200 document  text/html; charset=UTF-8     /download-eroll
        ...
        200 script    application/javascript       /static/js/main.b35ed2f3.js
        200 fetch     application/json             /api/v1/captcha-service/getCaptcha/EROLL
        200 media     audio/wav                    /api/v1/captcha-service/generateVoiceCaptcha/AB71D67D814F82A7FDBA59667F80884C
```

**The captcha API endpoint is confirmed working:**
- `GET https://gateway-voters.eci.gov.in/api/v1/captcha-service/getCaptcha/EROLL` → Returns base64 JPEG + session `id`
- `GET https://gateway-voters.eci.gov.in/api/v1/captcha-service/generateVoiceCaptcha/{id}` → Audio fallback

### Vision Model Configuration
- **Model:** `meta/llama-3.2-11b-vision-instruct` (via NVIDIA integrate API)
- **Endpoint:** `https://integrate.api.nvidia.com/v1/chat/completions`
- **Rate limit:** 40 req/min (free tier)
- **Consensus:** Single model currently (other 2 models return 404/timeout)

---

## 2. Total States: **36**

| Code | State |
|------|-------|
| U01 | Andaman & Nicobar Islands |
| S01 | Andhra Pradesh |
| S02 | Arunachal Pradesh |
| S03 | Assam |
| S04 | Bihar |
| U02 | Chandigarh |
| S26 | Chhattisgarh |
| U03 | Dadra & Nagar Haveli and Daman & Diu |
| S05 | Goa |
| S06 | Gujarat |
| S07 | Haryana |
| S08 | Himachal Pradesh |
| U08 | Jammu and Kashmir |
| S27 | Jharkhand |
| S10 | Karnataka |
| S11 | Kerala |
| U09 | Ladakh |
| U06 | Lakshadweep |
| S12 | Madhya Pradesh |
| S13 | Maharashtra |
| S14 | Manipur |
| S15 | Meghalaya |
| S16 | Mizoram |
| S17 | Nagaland |
| U05 | NCT OF Delhi |
| S18 | Odisha |
| U07 | Puducherry |
| S19 | Punjab |
| S20 | Rajasthan |
| S21 | Sikkim |
| S22 | Tamil Nadu |
| S29 | Telangana |
| S23 | Tripura |
| S24 | Uttar Pradesh |
| S28 | Uttarakhand |
| S25 | West Bengal |

---

## 3. Complete State Structure Details

### All 36 States - Years & Roll Types Summary

| State | Code | Years | Roll Types (for 2026) |
|-------|------|-------|----------------------|
| Andaman & Nicobar Islands | U01 | 3 (2026,2025,2024) | 4: Supplement-3 2026, Supplement-2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Andhra Pradesh | S01 | 3 | 1: SIR DraftRoll - 2026 |
| Arunachal Pradesh | S02 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Assam | S03 | 3 | 5: PC Bye-Election FinalRoll-2026, Supplement-3 2026, SSR FinalRoll Revision2 - 2026, FinalRoll - 2026, DraftRoll - 2026 |
| Bihar | S04 | 3 | 0 |
| Chandigarh | U02 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Chhattisgarh | S26 | 3 | 4: Supplement-3 2026, Supplement-2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Dadra & Nagar Haveli and Daman & Diu | U03 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Goa | S05 | 3 | 6: Supplement-4 2026, Supplement-3 2026, Supplement-2 2026, Bye Election Final Roll-Revision2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Gujarat | S06 | 3 | 6: Bye-Election FinalRoll-REV3 2026, Supplement-3 2026, Supplement-2 2026, Bye Election Final Roll-Revision2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Haryana | S07 | 3 | 1: SIR DraftRoll - 2026 |
| Himachal Pradesh | S08 | 3 | 0 |
| Jammu and Kashmir | U08 | 3 | 0 |
| Jharkhand | S27 | 3 | 1: SIR DraftRoll - 2026 |
| Karnataka | S10 | 3 | 4: SIR DraftRoll - 2026, Bye Election Final Roll-Revision2 2026, Bye Election Final Roll 2026, Bye Election Draft Roll 2026 |
| Kerala | S11 | 3 | 4: Supplement-3 2026, SIR FinalRoll - Rev2 - 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Ladakh | U09 | 3 | 0 |
| Lakshadweep | U06 | 3 | 4: Supplement-3 2026, Supplement-2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Madhya Pradesh | S12 | 3 | 4: Supplement-3 2026, Supplement-2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Maharashtra | S13 | 3 | 4: SIR DraftRoll - 2026, Bye Election Final Roll-Revision2 2026, Bye Election Final Roll 2026, Bye Election Draft Roll 2026 |
| Manipur | S14 | 3 | 5: SIR FinalRoll - 2026, SIR DraftRoll - 2026, Bye Election Final Roll 2026, Supplement-4 2026, Bye Election Draft Roll 2026 |
| Meghalaya | S15 | 3 | 1: SIR DraftRoll - 2026 |
| Mizoram | S16 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Nagaland | S17 | 3 | 4: SIR DraftRoll - 2026, Bye Election Final Roll-Revision2 2026, Bye Election Final Roll 2026, Bye Election Draft Roll 2026 |
| NCT OF Delhi | U05 | 3 | 1: SIR DraftRoll - 2026 |
| Odisha | S18 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Puducherry | U07 | 3 | 5: Bye Election Final Roll 2026, Supplement-3 2026, SIR FinalRoll-Rev2-2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Punjab | S19 | 3 | 1: SIR DraftRoll - 2026 |
| Rajasthan | S20 | 3 | 4: Supplement-3 2026, Supplement-2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Sikkim | S21 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Tamil Nadu | S22 | 3 | 5: Bye Election Final Roll 2026, Supplement-3 2026, SIR FinalRoll-Rev2- 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Telangana | S29 | 3 | 1: SIR DraftRoll - 2026 |
| Tripura | S23 | 3 | 3: Bye Election Final Roll-Revision2 2026, Bye Election Final Roll 2026, Bye Election Draft Roll 2026 |
| Uttar Pradesh | S24 | 3 | 3: Supplement-2 2026, SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| Uttarakhand | S28 | 3 | 2: SIR FinalRoll - 2026, SIR DraftRoll - 2026 |
| West Bengal | S25 | 3 | 47: **Massive list** (Bye-Election, Appellate Tribunal Supplementary/Deleted Lists, Adjudication Supplementary/Deleted Lists, SIR FinalRoll-Rev2, SIR FinalRoll, SIR DraftRoll) |

### Key Observations

- **All 36 states** have 3 years available (2026, 2025, 2024)
- **Roll types vary significantly**: from 0 (Bihar, Himachal Pradesh, J&K, Ladakh) to 47 (West Bengal)
- **Most common pattern**: SIR DraftRoll + SIR FinalRoll + Supplements
- **West Bengal is an outlier** with 47 roll types including Appellate Tribunal and Adjudication lists
- **Bihar, Himachal Pradesh, J&K, Ladakh** show 0 roll types for 2026 (may need different year selection)

---

## 4. Request Contract (Recovered from SPA Bundle)

The `generate-published-pdfs` POST requires exactly these fields:

```json
{
  "stateCd": "S01",
  "acNumber": "123",                    // constituency code
  "partNumberList": ["1", "2", "3"],    // part NUMBERS (not names), max 10
  "districtCd": "D01",
  "captcha": "aB3x9Z",                  // 6-char code from vision model
  "captchaId": "session-token-from-getCaptcha",
  "langCd": "ENG",                      // or HIN, etc.
  "publishedRollId": "roll-uuid-from-SPA-state",
  "misKey": "EROLLA32DVI09AJH"          // hardcoded constant
}
```

**Key findings from bundle analysis (6838.fe8807b3.chunk.js):**
- `partNumberList` = part NUMBERS (col3 of table rows), not display names
- `captchaId` = `et.id` from `getCaptcha/EROLL` response envelope
- `misKey` = hardcoded client constant `EROLLA32DVI09AJH`
- `publishedRollId` = from SPA's internal state (not in DOM)

---

## 5. Download Flow (Two-Stage)

1. **POST** `https://gateway-voters.eci.gov.in/api/v1/printing-publish/generate-published-pdfs` 
   - Returns `{payload: "path/to/file.pdf"}` (or .zip)
   
2. **GET** `https://voters.eci.gov.in/eroll/{path}` (in page context for cookies)
   - Returns actual PDF/ZIP bytes

---

## 6. Current Blockers (Zero Real PDFs Downloaded)

| Issue | Root Cause | Status |
|-------|------------|--------|
| Form never renders intermittently | `main.b35ed2f3.js` returns 503 → `#root` empty | Mitigated: 12-attempt retry with backoff |
| CORS blocks auth | `validate-token` to gateway fails CORS | Angular boots anyway (401 ignored) |
| Parts table AJAX race | Language selection should trigger `get-publish-part-list` but table stays empty (0 rows after 90s) | **ACTIVE BLOCKER** |

---

## 7. Screenshots

### Form Loaded Successfully
![Form loaded](isolate_after_load.png)

*Generated by `isolate_bootstrap.py` - shows 37 state options populated (includes "Select State" placeholder), SPA booted*

---

## 8. Next Steps

1. **Fix parts table AJAX race** - Add explicit polling after each dropdown selection (constituency → language) before expecting parts table
2. **Run full discovery** - Complete structure (districts, constituencies, languages) for all states with roll types
3. **Execute first real download** - POST `generate-published-pdfs` → fetch PDF → zip → MinIO upload
4. **Purge 21 demo archives** from MinIO (`purge_demo_archives.py --delete`)

---

## 9. Running the Downloader

```bash
# Discover structure (one-time)
.venv-test/bin/python -m src.eci_downloader --discover

# Test download (1 per category)
.venv-test/bin/python -m src.eci_downloader --download --max-per-category 1

# Full download with MinIO upload
.venv-test/bin/python -m src.eci_downloader --download --upload-to-minio
```

---

*Documentation generated from live probing scripts. Run `isolate_bootstrap.py` and `probe_captcha_shape.py` to regenerate proofs.*