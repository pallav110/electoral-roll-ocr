The self-test passes: 69 checks. The module compiles successfully. The findings below are additional issues visible from code inspection; no production edits were made.

1. Tesseract serial numbers are unconditionally discarded

Severity: High — correctness

Location: scripts/ocr_pdf_api.py:2123

record["sno"] = str(metadata.get("sno", ""))

Earlier, parse_voter_box_from_ocr_lines() can successfully populate record["sno"] from Tesseract:

m = re.search(r"\|\s*(\d{1,3})\s*\|", clean_value(line))

However, _extract_card() always replaces that value with Paddle’s result, even when Paddle returns an empty string because PaddleOCR is unavailable or failed.

Failure scenario

1. Tesseract reads serial 525.
2. PaddleOCR fails or is unavailable.
3. _extract_paddle_card_metadata() returns "sno": "".
4. _extract_card() overwrites Tesseract’s valid 525 with "".
5. Page-level reconciliation may repair it only if enough other cards p

Recommendation

Only overwrite the Tesseract serial when Paddle produced a non-empty va

paddle_sno = str(metadata.get("sno", "") or "")
if paddle_sno:
    record["sno"] = paddle_sno

This is particularly important because the service is documented as capable of operating with Tesseract when PaddleOCR is unavailable.

---

2. A page can be skipped because only the first three cards are probed

Severity: High — correctness

Location: scripts/ocr_pdf_api.py:2217-2224

for card_rect in _voter_card_rects(page)[:3]:
    image = getattr(page, "get_pixmap")(
        dpi=200, clip=card_rect, alpha=False).tobytes("png")
    if _extract_tesseract_epic(image):
        return True
return False

The page is classified as a voter page only when one of the first three cards has an EPIC that matches the strict Tesseract pattern.

Failure scenario

- The first row has poor print quality, a damaged crop, or an EPIC OCR failure.
- Cards in rows 2–10 contain valid voters.
- The probe returns False.
- The entire page is skipped and all 30 records are lost.

This is especially fragile because _extract_tesseract_epic() requires an exact three-letter/seven-digit format.

Recommendation

Use a bounded probe across multiple rows and columns, or use a looser voter-grid signal such as:

- at least two EPIC candidates across several positions;
- presence of multiple voter labels;
- a fallback Paddle EPIC check.

Do not require the first row to be readable.

---

3. Batch path processing has a TOCTOU sandbox race

Severity: High if the service is network-accessible — security

Locations:

- Validation: scripts/ocr_pdf_api.py:2803-2822
- Later worker read: scripts/ocr_pdf_api.py:2524-2534

The batch path endpoint validates and resolves paths before queuing them. The Celery worker later resolves and reads the path, but does not re-check
_path_is_allowed(source_path).

Failure scenario

1. An allowed PDF path is submitted.
2. The file is replaced by a symlink, junction, or another path before the worker reads it.
3. The worker follows the replacement path and processes a file outside the configured allowed roots.

Recommendation

Prefer staging/copying accepted files into a controlled job directory bevalidate the resolved source path in the worker and avoid relying on apath that can change between validation and open.

---

4. The default deployment exposes unauthenticated OCR and filesystem-related endpoints

Severity: High if bound beyond localhost — security

Locations:

- scripts/ocr_pdf_api.py:1424-1428
- scripts/ocr_pdf_api.py:3259-3263
- Batch path endpoint: scripts/ocr_pdf_api.py:2788-2845

The server defaults to:

host=os.getenv("OCR_API_HOST", "0.0.0.0")

CORS is also unrestricted:

allow_origins=["*"]
allow_methods=["*"]
allow_headers=["*"]

There is no authentication or authorization layer. By default, OCR_BATC the current working directory.

Impact

Any reachable client can:

- upload PDFs for expensive OCR processing;
- invoke the batch path endpoint against files under the allowed root;
- retrieve OCR results;
- consume CPU, memory, disk, and PaddleOCR resources.

This may be acceptable for a strictly local development service, but the default 0.0.0.0 binding makes accidental network exposure likely.

Recommendation

For production or shared networks:

- default to 127.0.0.1;
- require authentication for all OCR and batch endpoints;
- explicitly configure allowed roots;
- add request/rate/concurrency limits;
- avoid wildcard CORS unless the caller is authenticated and the origin is trusted.

---

5. Focused denominator repair can drop a recovered house prefix

Severity: Medium — correctness

Location: scripts/ocr_pdf_api.py:1882-1883

elif pad_denom.startswith(tess_denom) and len(pad_denom) == len(tess_denom) + 1:
    return tess_prefix + paddle_house, "paddle_repair", reasons

This branch uses tess_prefix but ignores focused_prefix.

Failure scenario

- Tesseract reads 1/8.
- Paddle reads 1/18.
- Focused OCR correctly detects the printed prefix इ-.
- tess_prefix is empty.
- The function returns 1/18 instead of इ-1/18.

The prefix is preserved in several other branches, so this is an inconsistent arbitration path.

Recommendation

Use the same prefix selection logic as the surrounding branches:

pfx = tess_prefix or focused_prefix
return pfx + paddle_house, "paddle_repair", reasons

---

6. Complete focused plot/address OCR is implemented but never used

Severity: Medium — missing recovery path

Location: scripts/ocr_pdf_api.py:433-469

_extract_tesseract_house_address() contains focused OCR logic for plot/no call to it anywhere in _extract_card() or the page pipeline.

The card pipeline calls:

- _extract_tesseract_house_prefix()
- _extract_tesseract_house_candidates()

but never _extract_tesseract_house_address().

Impact

If the primary parser misses a structured value such as:

पी. नं-बि 90, ख 4-707

the dedicated focused fallback cannot recover it. Numeric Paddle OCR may then provide only a partial numeric value.

Recommendation

Either:

1. call this function when the primary house value is missing or structured-looking but low quality; or
2. remove the dead helper and document that structured addresses depend entirely on the primary pass.

---

7. Upload batch filenames are not sanitized for Windows filesystem rules

Severity: Medium — reliability

Locations:

- scripts/ocr_pdf_api.py:2739-2759
- scripts/ocr_pdf_api.py:2542-2545

The normal /ocr/extract endpoint sanitizes the output stem, but /ocr/batch/upload does not sanitize client-provided filenames before using them in filesystem paths:

destination = input_dir / f"{index:04d}_{filename}"

Later, batch output names also use:

f"{index:04d}_{Path(filename).stem}_ocr.json"

Failure scenarios on Windows

A client filename containing any of the following can fail:

- :
- *
- ?
- <
- >
- |
- trailing dot or space
- reserved names such as CON.pdf

The upload endpoint can return an internal error after creating part of

Recommendation

Apply one shared filename sanitizer to all upload and output paths, not only the main extraction endpoint.

---

8. Failed batch uploads leave partial job directories and files

Severity: Medium — resource exhaustion

Location: scripts/ocr_pdf_api.py:2748-2785

If an upload exceeds the per-file or total batch limit after one or more chunks have been written, the endpoint raises HTTPException, but the partially populated BATCH_WORK_DIR / job_id is not deleted.

Impact

Repeated oversized or malformed requests can accumulate:

- partial PDFs;
- empty job directories;
- abandoned input files.

Because the service is unauthenticated, this becomes a disk-consumption

Recommendation

Wrap staging in an exception handler that removes the job directory on failure. Consider a background cleanup policy for abandoned jobs and old completed jobs.

---

9. The implementation currently performs per-card PDF rasterization sequentially

Severity: Medium — performance regression

Locations:

- scripts/ocr_pdf_api.py:2075-2079
- scripts/ocr_pdf_api.py:2358-2363

Every card performs two independent PDF rasterizations:

hindi_bytes = get_pixmap(dpi=200, clip=card_rect, alpha=False).tobytes("png")
metadata_bytes = get_pixmap(dpi=300, clip=card_rect, alpha=False).tobytes("png")

The page loop then processes all cards sequentially.

This conflicts with the project architecture documented in CLAUDE.md, which specifies:

- one full-page 300 DPI render;
- NumPy card slicing;
- concurrent card OCR.

Impact

For a 30-card page, this creates at least 60 card-level rasterizations, plus the three-card grid probe. It explains the observed 100+ second page timings and increases
memory/CPU pressure.

This is not a correctness blocker, but it is a substantial implementation regression.

Recommendation

Defer optimization until correctness is finalized, as planned, but eventually:

1. render the page once;
2. slice card regions from the rendered image;
3. use controlled card-level concurrency;
4. serialize Paddle calls with an explicit lock.

---

10. PaddleOCR initialization and calls are not explicitly synchronized

Severity: Medium if concurrency is reintroduced — concurrency correctne

Locations:

- scripts/ocr_pdf_api.py:475-490
- Paddle calls throughout metadata extraction

_PADDLE_SERIAL_OCR is lazily initialized without a lock:

if _PADDLE_SERIAL_OCR is None:
    _PADDLE_SERIAL_OCR = PaddleOCR(...)

The same instance is then used by _paddle_text() and _detect_deleted_wa

The current sequential card loop largely hides this problem. However, the project architecture explicitly describes concurrent card OCR and says PaddleOCR is not thread-safe.

Failure scenario after concurrency is enabled

- Two card workers observe _PADDLE_SERIAL_OCR is None.
- Both initialize separate model instances, or both call the same insta
- Paddle returns inconsistent results, raises internal errors, or consumes excessive memory.

Recommendation

Add:

- a one-time initialization lock;
- a separate _PADDLE_LOCK around every Paddle inference call.

---

11. The API exposes an absolute server filesystem path in every response

Severity: Low to Medium — information disclosure

Location: scripts/ocr_pdf_api.py:2643-2646

result["json_output_file"] = str(output_path)

Because OCR_OUTPUT_DIR is resolved to an absolute path, clients receivem path.

Impact

This leaks deployment details such as:

- usernames;
- directory structure;
- drive letters;
- container paths.

Recommendation

Return a logical identifier or relative output name instead. Keep the aogs or internal job state.

---

12. House-prefix normalization may corrupt legitimate numeric hyphenated values

Severity: Low to Medium — data corruption risk

Location: scripts/ocr_pdf_api.py:690-697

r"^(?:\$|§|=|8|5|ई|इ|F|(?:A\s+F)|[eEiI]{1,2})\s*-\s*(?=\d)"

This maps values such as:

8-481
5-484

to:

इ-481
इ-484

The rule is useful for observed OCR errors, but it treats a leading numeric 8 or 5 before a dash as proof of a short-i prefix.

Failure scenario

A genuine numeric/hyphenated address beginning with 8- or 5- is normalized into a Devanagari-prefixed address.

Recommendation

Require additional evidence before converting numeric prefixes, such as:

- focused prefix OCR;
- agreement with a known printed short-i glyph;
- a slash-form or card-template-specific pattern.

Avoid globally mapping every 5- or 8- house value.

Overall assessment

The relation and photo-mask changes are covered reasonably well by the trict slash-form arbitration correctly protects the observed 15/245versus 5/245 case.

The most important issues to fix before relying on the service broadly are:

1. preserve Tesseract serials when Paddle returns no serial;
2. make page detection tolerant of first-row OCR failures;
3. secure the unauthenticated network-facing endpoints;
4. close the batch path race;
5. sanitize and clean up batch upload files.

The performance architecture should be addressed afterward, since the current sequential per-card rendering is the main reason page processing remains above the desired
timing target.