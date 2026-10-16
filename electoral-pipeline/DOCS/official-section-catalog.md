# Official section catalogue

Use this only when a section number/name is available from an official ECI or
CEO source.  It is the safe completion path for a PDF whose printed header is
too degraded for OCR to read confidently.

The pipeline never creates catalogue rows from OCR output.  It applies a row
only when the filename, roll revision, state code, AC code, booth/part code,
and—when supplied—the source PDF SHA-256 all match exactly.  A mismatch leaves
the OCR result unchanged.

## Configure it

1. Create a JSON file outside the repository and restrict write access to an
   authorised administrator.
2. Validate it:

   ```bash
   docker compose exec web python tools/import_official_sections.py /data/official-sections.json
   ```

3. Set `OFFICIAL_SECTION_CATALOG_PATH=/data/official-sections.json` for the
   web and worker services, and mount that file read-only at the same path in
   each service that performs extraction.
4. Restart only the web and extraction-worker services after validation.

Do not enable an empty, guessed, or OCR-generated catalogue.  A catalogue is
an authoritative source, not a cache of OCR guesses.

## JSON shape

```json
{
  "sections": [
    {
      "source_filename": "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-301-WI.pdf",
      "state_code": "24",
      "ac_code": "53",
      "booth_code": "301",
      "roll_revision": "SIR-FinalRoll-Revision1",
      "section_number": "1",
      "section_name_hi": "उत्तरांचल कालोनी गली न0 10 से 11",
      "source_url": "https://official.example/roll-301.pdf",
      "source_sha256": "optional full sha256 of the official PDF"
    }
  ]
}
```

`source_url` must be the official provenance URL or an internal official
archive reference.  When a match is used, records receive
`anubhag_status=official_verified` and their field source identifies the
catalogue.  When no verified source exists, the underlying name stays unknown;
the UI/export status makes that explicit instead of pretending a value was
read.
