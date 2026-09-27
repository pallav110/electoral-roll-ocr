# Installing the Tesseract Devanagari Script Model on Ubuntu

This model improves Hindi name and relation recognition in the OCR pipeline
(used by `_extract_voter_name_with_tesseract_devanagari` and
`_extract_relation_text_with_tesseract_devanagari`). The pipeline falls back
gracefully when the model is absent.

## Prerequisites

```bash
sudo apt update
sudo apt install -y tesseract-ocr tesseract-ocr-hin tesseract-ocr-eng
```

Verify Tesseract is working:

```bash
tesseract --version
tesseract --list-langs | grep -E '^(hin|eng)$'
```

## Install the Devanagari script model (recommended)

The model lives in the official `tessdata_fast` repository under a subfolder
named `script`. The filename on disk is `Devanagari.traineddata`; Tesseract
discovers it as `script/Devanagari`.

```bash
# Find the system tessdata directory
TESSDATA=$(tesseract --list-langs 2>&1 | head -1 | sed -n 's|List of available languages in "\(.*\)":|\1|p')
echo "tessdata dir: $TESSDATA"

# Create the script subfolder if it does not already exist
sudo mkdir -p "$TESSDATA/script"

# Download the ~17 MB trained model
sudo wget -O "$TESSDATA/script/Devanagari.traineddata" \
  "https://github.com/tesseract-ocr/tessdata_fast/raw/main/script/Devanagari.traineddata"

# Verify ownership and permissions
sudo chown root:root "$TESSDATA/script/Devanagari.traineddata"
sudo chmod 644 "$TESSDATA/script/Devanagari.traineddata"
```

## Verify the installation

```bash
tesseract --list-langs | grep devanagari
```

Expected output includes `script/Devanagari`:

```
...
script/Devanagari
...
```

A quick smoke test:

```bash
echo "प्रसाद" | tesseract stdin stdout -l hin+eng+script/Devanagari --psm 7
```

The output should contain the Devanagari rendering of the word (not Latin
garbage such as `Pare`).

## What the pipeline does when the model is missing

If `script/Devanagari` is not installed, the module probes Tesseract once at
startup and sets `_DEVANAGARI_AVAILABLE = False`. All three focused passes
(`_extract_voter_name_with_tesseract_devanagari`,
`_extract_relation_text_with_tesseract_devanagari`, and
`_merge_devanagari_name_and_relation`) become no-ops and the existing
`hin+eng` baseline continues to work unchanged. No hard-fail occurs.

## Optional: keep the model in sync

The upstream `tessdata_fast` repo publishes periodic releases. To update:

```bash
sudo wget -O "$TESSDATA/script/Devanagari.traineddata" \
  "https://github.com/tesseract-ocr/tessdata_fast/raw/main/script/Devanagari.traineddata"
```

## Troubleshooting

**`script/Devanagari` does not appear in `--list-langs`**

- Confirm the file actually exists:
  ```bash
  ls -lh "$(tesseract --list-langs 2>&1 | head -1 | sed -n 's|.*"\(.*\)":|\1|p')/script/Devanagari.traineddata"
  ```
- Restart the API process — the availability probe runs once at module
  import time and caches the result.
- Ensure the file is readable: `sudo chmod 644 .../script/Devanagari.traineddata`.

**Model downloaded but OCR still returns Latin noise for Hindi names**

- Check that the language string in the code is exactly
  `hin+eng+script/Devanagari` (forward slashes, not backslashes). The probe
  normalises both, but a typo in the lang string will fall back silently.
- The focused crops are narrow (name band and relation band only). If the
  card template has changed, the fixed ratios `_DEVANAGARI_NAME_REGION` and
  `_DEVANAGARI_RELATION_REGION` may need recalibration — see
  `tests/test_scripts/trace_card_pipeline.py` for diagnostics.

**`wget` is blocked by a proxy**

Replace `wget` with `curl`:

```bash
sudo curl -L -o "$TESSDATA/script/Devanagari.traineddata" \
  "https://github.com/tesseract-ocr/tessdata_fast/raw/main/script/Devanagari.traineddata"
```
