# Standalone Electoral-Roll OCR API

## Purpose

`pipeline/ocr_pdf_api.py` is a self-contained FastAPI service for extracting
structured voter records from the supported Hindi electoral-roll PDF layout.

The Python file contains all repository-specific extraction code internally:

- Measured voter-card crop geometry.
- Tesseract Hindi and English OCR.
- Focused EPIC extraction.
- PaddleOCR serial, house-number, and deletion metadata extraction.
- Hindi voter-field parsing and normalization.
- OCR-result arbitration.
- Page detection and page-range validation.
- Serial-sequence reconciliation with correction audit metadata.
- FastAPI routes and a direct Uvicorn launcher.

It does not import `pipeline.pdf_extract` or any other local project module.
This means the API can be handed to another user as one Python source file.
External Python packages, Tesseract, and the Hindi Tesseract language data are
still required on the destination machine.

## Files

Primary handoff file:

```text
pipeline/ocr_pdf_api.py
```

Optional dependency reference:

```text
pipeline/ocr_api_requirements.txt
```

The requirements file is only an installation convenience. The API does not
read or import it at runtime.

## System requirements

- Python 3.12 was used for the validated run.
- Tesseract OCR executable.
- Tesseract Hindi language data.
- Sufficient RAM for PaddleOCR.
- CPU execution is supported; a GPU is not required by this API.

On Debian or Ubuntu, the system OCR packages can normally be installed with:

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-hin
```

Verify that Hindi is available:

```bash
tesseract --list-langs
```

The output must contain both `eng` and `hin`.

## Python dependencies

When the optional requirements file is available:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r pipeline/ocr_api_requirements.txt
```

For a one-file handoff, install the dependencies directly:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install \
  fastapi==0.115.0 \
  uvicorn==0.32.0 \
  python-multipart==0.0.12 \
  pymupdf==1.24.10 \
  pytesseract==0.3.13 \
  Pillow==10.4.0 \
  numpy==2.2.6 \
  opencv-python==5.0.0.93 \
  opencv-contrib-python==5.0.0.93 \
  paddleocr==2.10.0 \
  paddlepaddle==3.0.0
```

PaddlePaddle installation can vary by operating system and CPU/GPU platform.
Use the appropriate PaddlePaddle package for the destination environment when
the exact command above is not compatible.

## Starting the API

Run the Python file directly:

```bash
source .venv/bin/activate
python pipeline/ocr_pdf_api.py
```

The default server address is:

```text
http://0.0.0.0:8082
```

The host and port can be changed with environment variables:

```bash
OCR_API_HOST=127.0.0.1 \
OCR_API_PORT=9000 \
python pipeline/ocr_pdf_api.py
```

Alternatively, start the application with Uvicorn from the directory containing
the file:

```bash
uvicorn ocr_pdf_api:app --host 0.0.0.0 --port 8082
```

## Interactive documentation

After the server starts, open:

```text
http://localhost:8082/docs
```

The Swagger page allows a user to select a PDF, enter a page range or enable
whole-PDF mode, submit the request, and inspect the JSON response without
writing client code.

## Health check

Request:

```bash
curl http://localhost:8082/health
```

Example response:

```json
{
  "status": "ok",
  "engine": "tesseract+paddleocr",
  "busy": false,
  "max_pdf_mb": 100
}
```

`busy: true` means another OCR request currently holds the extraction lock.

## Extraction endpoint

```text
POST /ocr/extract
Content-Type: multipart/form-data
```

### Form fields

| Field | Type | Required | Description |
|---|---|---:|---|
| `pdf_file` | PDF file | Yes | The electoral-roll PDF to upload. |
| `start_page` | Integer | Range mode | First page to process, 1-based and inclusive. |
| `end_page` | Integer | No | Last page, 1-based and inclusive. Defaults to `start_page`. |
| `whole_pdf` | Boolean | No | Process the entire PDF. Defaults to `false`. |
| `skip_non_voter_pages` | Boolean | No | Skip pages that do not match the expected voter grid. Defaults to `true`. |

`whole_pdf=true` cannot be combined with `start_page` or `end_page`.

### Extract one page

```bash
curl -X POST http://localhost:8082/ocr/extract \
  -F "pdf_file=@2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf" \
  -F "start_page=3"
```

When `end_page` is omitted, only `start_page` is processed.

### Extract an inclusive page range

```bash
curl -X POST http://localhost:8082/ocr/extract \
  -F "pdf_file=@2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf" \
  -F "start_page=3" \
  -F "end_page=5"
```

This processes pages 3, 4, and 5.

### Extract the whole PDF

```bash
curl -X POST http://localhost:8082/ocr/extract \
  -F "pdf_file=@2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf" \
  -F "whole_pdf=true"
```

With the default `skip_non_voter_pages=true`, the API probes the first three
expected card positions before running the expensive page extraction. Cover and
summary pages without a valid EPIC in those positions are skipped.

To force processing of every selected page:

```bash
curl -X POST http://localhost:8082/ocr/extract \
  -F "pdf_file=@roll.pdf" \
  -F "start_page=3" \
  -F "end_page=3" \
  -F "skip_non_voter_pages=false"
```

Only disable the page filter when the selected page is known to use the same
voter-card layout.

## Page-range validation

The API rejects invalid selections with HTTP 422, including:

- Missing `start_page` when `whole_pdf` is false.
- `start_page` below 1.
- `end_page` below `start_page`.
- `end_page` greater than the PDF page count.
- A page range combined with `whole_pdf=true`.

Example error:

```json
{
  "detail": "end_page must be greater than or equal to start_page"
}
```

Non-PDF filenames return HTTP 415. Empty files return HTTP 400. Files larger
than the configured limit return HTTP 413.

## Response structure

The response contains extraction metadata and a flat `records` list:

```json
{
  "ok": true,
  "filename": "roll.pdf",
  "page_count": 22,
  "selection": {
    "mode": "page_range",
    "start_page": 3,
    "end_page": 3,
    "pages_requested": 1,
    "skip_non_voter_pages": true
  },
  "pages_processed": 1,
  "pages_skipped": 0,
  "processed_page_details": [
    {
      "page_number": 3,
      "records": 30,
      "serial_corrections": 0,
      "elapsed_seconds": 40.0
    }
  ],
  "skipped_page_details": [],
  "serial_corrections_count": 0,
  "serial_correction_details": [],
  "records_count": 30,
  "elapsed_seconds": 41.0,
  "records": []
}
```

Each voter record includes:

```json
{
  "sno": 5,
  "voter_sr_no": "5",
  "id_card_no": "AWX5012331",
  "gender": "महिला",
  "age": "47",
  "house_no": "1/1044",
  "voter_first_name": "कल्पना",
  "voter_middle_name": "",
  "voter_sur_name": "",
  "relation_name": "पति",
  "voter_husband_first_name": "मनोज",
  "voter_husband_middle_name": "कुमार",
  "voter_husband_last_name": "शर्मा",
  "voter_father_first_name": "",
  "voter_father_middle_name": "",
  "voter_father_last_name": "",
  "voter_mother_first_name": "",
  "voter_mother_middle_name": "",
  "voter_mother_last_name": "",
  "voter_other_first_name": "",
  "voter_other_middle_name": "",
  "voter_other_last_name": "",
  "is_deleted": false,
  "state_code": "24",
  "ac_code": "53",
  "anubhag_code": "1",
  "anubhag_name": "उत्तरांचल कालोनी गली न0 8 से 9",
  "booth_code": "300",
  "pdf_name": "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
}
```

### Record numbering

- `sno` is an integer API-result counter starting at 1 for every request.
- `voter_sr_no` is the voter serial printed in the electoral roll. It remains a
  string so OCR formatting and any leading zero can be preserved.
- `page_number` and `card_index` are internal processing values and are not
  returned in voter records.

### Relation-name fields

The previous full-name fields are not returned:

```text
voter_husband_name
voter_father_name
voter_mother_name
voter_other_name
```

Each possible relation has first, middle, and last-name fields instead. A
one-word name is stored as the first name. A two-word name is split into first
and last. For three or more words, the first and last tokens become the first
and last names, and all intervening tokens form the middle name.

All husband, father, mother, and other component fields are included in every
record. Fields for non-applicable relation types remain empty.

### Roll metadata extraction

The API reads PDF-level metadata once per uploaded PDF. It does not repeat
this OCR for every voter card:

- Page 1: PaddleOCR reads `S24` from the heading and stores only `24` as
  `state_code`.
- Page 3: PaddleOCR extracts the numeric `ac_code`, `anubhag_code`, and
  `booth_code`.
- Page 3: Tesseract with `hin+eng` extracts the Hindi `anubhag_name`.
- The five extracted values are copied into every voter record returned for
  the selected range or whole PDF.
- The same values are also returned once at top level in `roll_metadata`.

For the supplied PDF, the extracted values are:

```json
{
  "state_code": "24",
  "ac_code": "53",
  "anubhag_code": "1",
  "anubhag_name": "उत्तरांचल कालोनी गली न0 8 से 9",
  "booth_code": "300"
}
```

The focused header crops are calibrated for this electoral-roll template. A
PDF with a different header layout may require recalibrating those crop ratios.

`pdf_name` is populated automatically from the uploaded source PDF filename.
Only the basename is returned; client directory paths are removed.

## Serial reconciliation

PaddleOCR can occasionally omit the leading digit from a three-digit serial,
for example `497` becoming `97`. The API calculates a candidate page base from:

```text
serial number - card index + 1
```

A correction is applied only when one page base has strict-majority support.
Every changed value is reported rather than silently hidden:

```json
{
  "sno": 497,
  "ocr_value": "97",
  "corrected_value": "497"
}
```

If no strict majority exists, the API does not alter the serial values.

## File-size configuration

The default upload limit is 100 MB. Override it with `MAX_OCR_PDF_BYTES`:

```bash
MAX_OCR_PDF_BYTES=209715200 python pipeline/ocr_pdf_api.py
```

The example above permits files up to 200 MB.

## Concurrency and runtime

OCR work is CPU-intensive and the shared PaddleOCR instance is not treated as
thread-safe. The API serializes extraction requests with one asynchronous lock.
Additional requests wait until the current extraction finishes.

Measured results on the reference machine:

- One 30-card page: approximately 41–43 seconds.
- Complete 22-page reference PDF: 818.8 seconds.
- Voter pages processed: 3 through 21.
- Non-voter pages skipped: 1, 2, and 22.
- Records produced: 531.

For higher-volume production use, put requests behind a job queue and run one
OCR worker per safe PaddleOCR process. Do not simply run card extraction in
multiple threads against the same global OCR instance.

## Validation performed

The standalone source file was copied outside the repository and imported from
`/tmp` without access to `pipeline.pdf_extract`. It successfully:

- Started as an independent FastAPI application.
- Returned HTTP 200 from `/health`.
- Processed page 3 through a multipart API request.
- Returned 30 page-3 records.
- Matched the validated `page3_ocr_results.json` field-for-field.
- Processed the complete reference PDF into 531 records.
- Produced a continuous, unique `sno` counter and `voter_sr_no` sequence from
  1 through 531 after ten reported voter-serial corrections.
- Produced 531 EPIC values matching the expected three-letter/seven-digit
  format.
- Returned every `is_deleted` field as a JSON boolean.

The complete reference output is stored locally as:

```text
whole_pdf_ocr_results.json
```

The full output passed structural validation. Hindi voter fields outside the
manually reviewed page 3 have not received equivalent field-by-field human
ground-truth validation.

## Template limitation

The crop geometry is calibrated for the current electoral-roll template. A PDF
with different page dimensions, margins, row heights, column gaps, or card
layout may require new measured boundaries.

Before using a new template in production:

1. Render boundary overlays.
2. Confirm that the first row maps to printed cards 1, 2, and 3.
3. Confirm all columns and rows remain inside their card borders.
4. Run a manually reviewed sample page.
5. Compare the API output against source-card ground truth.

## Deployment safety

The current service enables permissive CORS for convenient local integration.
Before exposing it to an untrusted network:

- Restrict allowed origins.
- Add authentication and authorization.
- Place the service behind HTTPS and a reverse proxy.
- Configure request and proxy timeouts for long OCR jobs.
- Limit upload size appropriately.
- Avoid logging uploaded voter data or complete API responses.
- Define retention and deletion rules for PDFs and extracted personal data.

