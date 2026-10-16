# OCR Technical Stack

## Purpose

This repository extracts voter records from Hindi electoral-roll PDFs. The current OCR pipeline processes one measured voter-card crop at a time and writes structured JSON records.

The design separates OCR responsibilities:

- **Tesseract** extracts Hindi voter text.
- **PaddleOCR** extracts small machine-readable metadata and numeric regions.
- **PyMuPDF** renders PDF pages and creates the card crops.
- **OpenCV and NumPy** preprocess image regions.
- **Rule-based Python parsing** maps OCR lines into the final JSON schema.

The local pipeline runs inside `pipeline/.venv`.

The distributable API is implemented as the standalone
`pipeline/ocr_pdf_api.py` file. It embeds the required extraction and parsing
functions so its recipient does not need to import `pipeline/pdf_extract.py`.

## Main Components

### `pipeline/pdf_extract.py`

This is the main extraction module. It contains:

- PDF rendering through `fitz` / PyMuPDF.
- Validated voter-card boundary geometry.
- Tesseract Hindi OCR.
- PaddleOCR metadata extraction.
- OCR candidate selection.
- Rule-based field parsing.
- Relation-field normalization.
- Deleted-card detection support.

Important functions:

- `_voter_card_rects(page)` returns the 30 measured card rectangles.
- `_iter_voter_card_images(page)` renders card crops for the general extraction paths.
- `_extract_text_with_tesseract(img_bytes)` extracts Hindi and English text.
- `_extract_paddle_card_metadata(img_bytes)` extracts serial, EPIC, house-number fallback, and deletion status.
- `parse_voter_box_from_ocr_lines(lines)` converts OCR lines into a voter record.

### `test_scripts/run_page3_ocr_export.py`

This is the current page-3 OCR export runner. It uses two image resolutions intentionally:

```text
200 DPI card crop -> Tesseract Hindi OCR
300 DPI card crop -> PaddleOCR metadata OCR
```

It writes the result to:

```text
page3_ocr_results.json
```

## Card Boundary Geometry

The card layout was calibrated visually against the printed electoral-roll page and then transferred into `pdf_extract.py`.

The page uses:

- 3 columns per row.
- 10 rows per full voter page.
- Explicit measured top and bottom ratios for each row.
- Explicit horizontal column gaps.
- Separate card rectangles rather than one equal-height grid.

This matters because equal-height rows caused the crop boundaries to accumulate error and eventually shift into the whitespace between printed rows.

The production rectangle helper is the authoritative implementation used by OCR and vision card extraction paths.

## OCR Responsibilities

### Tesseract: Hindi and full-card text

Tesseract uses the installed `hin+eng` language data and extracts:

- Voter name.
- Father, husband, mother, or other relation name.
- Relation label.
- House number.
- Age.
- Gender.
- General Hindi card text.

The Hindi path uses the 200 DPI card image because that produced better Hindi text than the 300 DPI full-card path.

The current Tesseract implementation evaluates two image variants:

1. Grayscale image.
2. Adaptive-threshold image.

Both use Tesseract `--oem 3 --psm 6`. A rule-based score favors candidates containing Hindi field labels and Devanagari text. This improves cases such as:

```text
पिता का नाम: सुमेर चन्द
```

being selected instead of a corrupted candidate such as `BAR ae`.

### PaddleOCR: small numeric and metadata regions

PaddleOCR uses the English model for regions where Hindi recognition is not required:

- `sno`: serial number in the small green box.
- `id_card_no`: EPIC / voter ID in the top-right header.
- `is_deleted`: diagonal deleted watermark across the card.
- `house_no`: focused numeric fallback when Tesseract leaves the house number empty.

The high-resolution image is used only for these metadata regions. PaddleOCR is initialized once and reused for the page run.

Serial and EPIC regions are cropped independently from the full card. Serial candidates are digit-filtered, while EPIC candidates must match an uppercase-letter-plus-digit pattern.

Deleted detection uses full-card PaddleOCR text plus fuzzy matching so readings such as `iELETED` or `ETED` can identify the printed `DELETED` watermark.

## Hindi Field Parsing

`parse_voter_box_from_ocr_lines()` uses field labels and truncates values at the next known label. It does not rely on fixed word positions across the whole card.

Name splitting rules:

- One word: first name only.
- Two words: first name plus surname.
- Three or more words: first name, middle name, and surname.

Relation mapping uses four explicit fields:

- `पति` -> `voter_husband_name`
- `पिता` -> `voter_father_name`
- `माता` -> `voter_mother_name`
- `अन्य` -> `voter_other_name`

Legacy `voter_fathers_name` and `relative_*` fields are not emitted by the normalized OCR export.

## Output Schema

Each record contains fields such as:

```json
{
  "sno": "5",
  "id_card_no": "AWX5012331",
  "gender": "महिला",
  "age": "47",
  "house_no": "1/1044",
  "voter_first_name": "कल्पना",
  "voter_middle_name": "",
  "voter_sur_name": "",
  "relation_name": "पति",
  "voter_husband_name": "मनोज कुमार शर्मा",
  "voter_father_name": "",
  "voter_mother_name": "",
  "voter_other_name": "",
  "is_deleted": false,
  "card_index": 5
}
```

`is_deleted` is always a JSON boolean in the current exporter:

```json
true
```

or:

```json
false
```

## Runtime

The latest measured run processed one page containing 30 cards in:

```text
33.04 seconds total
approximately 1.10 seconds per card
approximately 1.1-1.12 seconds per card in normal runs
```

This is substantially faster than the previous vision-model flow, which could take roughly 2.5 minutes per card.

The runtime includes:

- 200 DPI Tesseract rendering and Hindi OCR.
- 300 DPI PaddleOCR rendering and metadata OCR.
- Serial, EPIC, deletion, and house-number region passes.
- JSON serialization.

## Running the Page-3 Export

```bash
cd /home/spx015/whisper-local
source pipeline/.venv/bin/activate
PYTHONPATH=. python test_scripts/run_page3_ocr_export.py
```

The runner prints:

- Record count.
- Elapsed seconds.
- Number of nonempty serial values.
- Serial values in card order.
- Output path.

## Debugging and Validation Tools

### Boundary overlays

`test_scripts/debug_first_three_ocr.py` renders card boundaries and serial-box boundaries. It also saves raw OCR output and card crops.

`test_scripts/test_whole_pdf_boundaries.py` renders red card and green serial boundaries for every PDF page.

### First-six OCR inspection

`test_scripts/ocr_first_six.py` saves card images, serial crops, and a raw OCR report for cards 1 through 6.

### Output validation

Useful checks include:

```bash
jq 'length' page3_ocr_results.json
jq 'all(.[]; (.is_deleted | type) == "boolean")' page3_ocr_results.json
jq '[.[] | select(.card_index == 16)]' page3_ocr_results.json
```

## Production Safety Notes

OCR output must still be validated before unattended production storage. Small printed serial digits, diagonal deletion stamps, and similar-looking EPIC characters can produce ambiguous OCR candidates.

Recommended production controls:

- Keep `card_index` so every result can be traced to its crop.
- Preserve the original card crop for review.
- Reject or review low-confidence serial and EPIC candidates.
- Do not infer missing Hindi values from unrelated fields.
- Keep the measured boundary geometry unchanged unless the PDF template changes.
- Record runtime and error counts for every batch.

## Look-Alike Glyph and Numeric Corrections

The standalone API uses evidence-based arbitration for visually similar OCR
glyphs instead of global character replacement:

- Focused 300-DPI Tesseract output does not replace valid primary Hindi merely
  because the strings differ by one glyph. A focused voter name is accepted
  only when the primary name is missing or both passes agree on the first
  name. A focused relation may also replace a conflicting value when its
  surname independently matches the voter's surname.
- The focused relation pass normalizes the `ATA` OCR reading of `नाम` before
  parsing.
- PaddleOCR reads isolated house and age regions at multiple scales. House
  candidates are not allowed to overwrite Tesseract merely because they are
  longer.
- A Paddle age replaces a one-digit Tesseract age only when the candidate is
  exactly the same value with a missing leading `1`, such as `9` to `19`.
  A general `1` versus `7` substitution is not safe.
- Confirmed tokens `तुबार`, `लाटयान`, and `नविश्ञ` are normalized to
  `तुषार`, `लाट्यान`, and `नविश`. No global
  `ब`/`व`, `ग`/`म`, or `ल`/`त` replacement is performed because either glyph
  can be correct in a real name.

Validated examples include house numbers `1030`, `1053`, and `1160`, plus
record 497 age `19` and address `इ-10/602`.

### Whole-PDF regression and selective rollback

A whole-PDF rerun after the first look-alike-glyph implementation was compared
with the preceding output. It changed 293 records and 336 fields, including
265 house numbers. Many were regressions caused by broad replacement rules,
for example `8/444` to `448/444`, `8/468` to `48/468`, and ages `47`
or `37` to `41` or `31`. Runtime also increased from 818.8 seconds to
940.33 seconds (14.84%).

The API therefore uses a selective rollback rather than reverting every useful
fix. It retains the confirmed records 284, 431, 433, 488, and 497, removes
generic one-glyph Hindi replacement and generic `1`/`7` age replacement,
and requires agreement or independent format evidence before Paddle changes a
house number. The comparison outputs are retained under `results/`; rerunning
the API does not modify them unless an output file is explicitly selected by
the caller.

## One-Time Electoral-Roll Header Metadata

`_extract_state_code(page)` extracts the two-digit state code from the `S24`
heading on page 1. `_extract_roll_header_metadata(page)` extracts the remaining
PDF-level metadata from page 3. `extract_pdf_ocr()` calls both once per request,
before voter-card processing, and copies the combined result into every
returned voter record. If a PDF has fewer than three pages, page 1 is used as
the fallback roll-metadata page as well.

The OCR engines are divided by content type:

- PaddleOCR reads `S24` from the focused page-1 heading, removes `S`, and
  stores `24` as `state_code`.
- PaddleOCR reads the focused page-3 numeric regions for `ac_code`,
  `anubhag_code`, and `booth_code`.
- Tesseract `hin+eng` with page segmentation mode 7 reads the focused Hindi
  `anubhag_name` line.

For the supplied roll, the header text produces:

```json
{
  "state_code": "24",
  "ac_code": "53",
  "anubhag_code": "1",
  "anubhag_name": "उत्तरांचल कालोनी गली न0 8 से 9",
  "booth_code": "300"
}
```

These values also appear once in the response-level `roll_metadata` object.
The page-3 crop ratios are template-specific and must be recalibrated if a
future electoral-roll PDF uses a different header layout.

## Page-3 Accuracy Issues and Resolutions

An accuracy audit of the 30 voter cards on page 3 initially found 21 field
mismatches across 13 cards. The failures came from three different layers:
parser cleanup, OCR-engine arbitration, and pixel-level character confusion.

Card 1 contains a diagonal `DELETED` watermark. Its voter fields are not part
of the current completeness target, but the record is retained and must have
`is_deleted: true`. Deleted status remains a required output signal.

### Parser and Unicode issues

The following deterministic problems were corrected in
`pipeline/pdf_extract.py`:

- Leading commas, colons, visarga characters, quotes, and border punctuation
  are removed from relation names. This corrected values such as
  `, लखपत सिंह` and `ः मगेन्द्र सिंह`.
- Zero-width joiners, zero-width non-joiners, zero-width spaces, and soft
  hyphens are removed from parsed values.
- Duplicate virama marks exposed after zero-width-character removal are
  collapsed. For example, `मोन्‍्टी` is normalized to `मोन्टी`.
- Known OCR variants of the house label are accepted. In addition to
  `मकान संख्या`, the parser recognizes `मक्कान संख्या` and `भ्रकान संख्या`.
- The OCR relation-label variant `प्रिता का नाम` is treated as
  `पिता का नाम`.

### House-number arbitration

PaddleOCR sometimes copied neighboring pixels into the beginning of a house
number, producing values such as:

| Correct value | Incorrect Paddle value |
|---|---|
| `8/444` | `448/444` |
| `8/468` | `48/468` |
| `8/829` | `48/829` |
| `09` | `409` |

The exporter now trusts a valid Tesseract address by default. Paddle may repair
a slash address only when both denominators agree and the Paddle numerator is
exactly the Tesseract numerator with a missing leading `1`; this repairs
`इ-0/602` to `इ-10/602` without turning `8/444` into `448/444`.

For plain numeric houses, Paddle is accepted only for a missing leading `1`,
a single `1`/`7` disagreement, or a value independently repeated at both
scales with the expected label/value format. If Tesseract is empty, the Paddle
candidate must likewise have repeated high-confidence or strong-format
support. A lone `|`, `[`, or `]` immediately before the photo field is
treated as digit `1`, because Tesseract uses these glyphs for a narrow printed
one.

### EPIC number extraction

The dedicated PaddleOCR EPIC crop produced several character confusions:

| Card | Correct EPIC | Previous Paddle result |
|---:|---|---|
| 7 | `GKM6453740` | `GK06453740` |
| 25 | `AWX4972535` | `AWX4972539` |
| 28 | `AWX2531556` | `AWX2531550` |
| 30 | `AWX1551183` | `AWX1651183` |

A dedicated grayscale Tesseract pass now reads only the EPIC header region
from the 200 DPI card. A candidate is accepted only when it matches the normal
three-letter/seven-digit shape. PaddleOCR and the full-card result remain
fallbacks when the strict Tesseract pass finds no valid EPIC.

### Hindi name and relation extraction

The old focused relation pass used a narrow 300 DPI crop with Tesseract page
segmentation mode 7. It could replace a correct full-card result with a worse
reading, including `PRETO सिंह`, `रणघीर सिंह`, and `महेश कुमार श्षर्मा`.

The focused pass now:

1. Downscales the 300 DPI card to a 200 DPI equivalent.
2. Uses a wider crop containing both the voter-name and relation-name lines.
3. Uses Tesseract page segmentation mode 6 for contextual line recognition.
4. Merges a focused name only when its first name matches the full-card result
   and all returned name parts are free of ASCII OCR noise.
5. Merges a focused relation only when exactly one relation field is populated,
   its value is clean Hindi, and the primary value is missing/noisy or the
   focused surname independently agrees with the voter's surname.

Boundary punctuation such as `!` or the Devanagari visarga `ः` is stripped
before names are split. A punctuation-only Devanagari token is not considered
a valid Hindi name. This prevents outputs such as husband first name `!` or
father first name `ः`.

This corrected the audited Hindi fields:

- Card 9 husband name: `किशनपाल सिंह`.
- Card 10 father name: `रणधीर सिंह`.
- Card 18 father name: `लखपत सिंह`.
- Card 21 voter name: `मोन्टी`.
- Card 23 surname and father name: `बालियान`, `मगेन्द्र सिंह`.
- Card 25 father name: `कैलाश गिरी`.
- Card 26 father name: `जगदीश`.
- Card 28 husband name: `महेश कुमार शर्मा`.

### Corrected-run validation

The corrected page-3 export completed in 42.84 seconds and produced 30
records. Validation confirmed:

- All reported non-deleted-card mismatches match their expected values.
- Serial numbers are present and ordered from `1` through `30`.
- Every EPIC matches the three-letter/seven-digit format.
- Every `is_deleted` value is a JSON boolean.
- Card 1 is the only deleted card and has `is_deleted: true`.
- No non-deleted record is missing its voter name, age, gender, house number,
  or relation type.
- No ASCII contamination remains in the Hindi name fields.
- The final whole-page sanity audit reported zero issues.

## Card Indexing and PyMuPDF Crop-Origin Fix

### Observed symptom

The first three expected voter cards were not being selected. The marked crops
contained printed serial numbers `4`, `5`, and `6` instead of `1`, `2`, and
`3`. The same offset appeared in both OCR mode and ML/vision mode.

This was not a recognition error and was not caused by PyMuPDF (`fitz`). It was
a shared crop-coordinate bug.

### Root cause

Both extraction modes ultimately depend on the card rectangles returned by
`_voter_card_rects(page)`:

- The OCR exporter renders each rectangle and sends the crop to Tesseract and
  PaddleOCR.
- The ML/vision path obtains its card images through
  `_iter_voter_card_images(page)`, which uses the same rectangle helper.

The earlier grid used a vertical start near `12%` of the page height. On an
842-point page this is approximately:

```text
842 * 0.12 = 101.04 points
```

That position was already inside the second printed voter row. The entire
first row was skipped, so the first three extracted images contained printed
cards 4, 5, and 6. OCR and the vision model correctly processed the pixels they
received; they had been given the wrong regions of the page.

PyMuPDF does not discover semantic card boundaries automatically. A call such
as:

```python
page.get_pixmap(dpi=dpi, clip=card_rect)
```

faithfully renders the supplied `clip` rectangle. Therefore, changing OCR
settings or the ML prompt could not repair this problem. The source rectangle
had to be corrected first.

### Correction and final geometry

The initial correction moved the grid start from `12%` to approximately `6%`,
which brought the first printed row back into the crop sequence. The layout was
then calibrated more precisely and the equal-height grid was replaced with ten
explicit measured row bounds.

The current authoritative first-row bounds in `pipeline/pdf_extract.py` are:

```python
(0.0325, 0.1209)
```

The remaining rows also have explicit top and bottom ratios. This avoids both
the original one-row offset and the cumulative drift produced by assuming ten
perfectly equal-height rows.

The horizontal layout is likewise measured using:

```text
left margin  = 0.011 of page width
right margin = 0.019 of page width
column gap   = 0.0055 of page width
```

The debug overlay in `test_scripts/debug_first_three_ocr.py` uses the same row
bounds and equivalent horizontal margins. Its labels use the mapping:

```python
index = row * 3 + column + 1
```

Consequently, the first physical row is marked as cards 1, 2, and 3, the
second as 4, 5, and 6, and so on.

### Why extraction worked only after the boundary fix

Before the correction, `fitz` was successfully rendering the requested area,
but that area began too low. The missing records were outside the supplied
clip and were never present in the image passed to either OCR or the ML model.
After the crop origin was corrected, the first row became part of the rendered
image, allowing both modes to extract cards 1, 2, and 3 correctly.

This distinction is important when debugging future page templates:

1. Verify the red outer card boundaries visually before tuning OCR or prompts.
2. Confirm that boundary indices match the serial numbers printed inside the
   cards.
3. Treat `_voter_card_rects(page)` as the production source of truth.
4. Update the debug overlay whenever production geometry changes.
5. Recalibrate the measured ratios if a different electoral-roll template has
   different margins, row heights, or inter-row spacing.
