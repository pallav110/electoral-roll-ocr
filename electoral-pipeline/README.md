# Electoral PDF Processing Pipeline

This is a runnable first implementation of the team plan. PostgreSQL owns all document, session and unit state; Redis/Celery carries ID-only jobs. The included extraction adapter returns **synthetic sample voters** so you can exercise the whole pipeline before connecting your existing extraction service. No sample record is derived from the PDF.

## Start

1. Install Docker Desktop and start its engine.
2. From this directory, run `cp .env.example .env` and change `ADMIN_TOKEN` in `.env`.
3. Run `docker compose up --build -d`.
4. The included blank demo PDF can be used immediately, or add your own PDF files to `sample-pdfs/`.
5. Open [the local dashboard](http://localhost:8088), signing in as `admin` with your `ADMIN_TOKEN`. Use **Discover PDFs now** for an immediate scan. Celery Beat also scans hourly.

The dashboard listens on `127.0.0.1:8088` by default. Change `WEB_PORT` if occupied. Run `docker compose logs -f worker beat web` to follow processing. Run `docker compose down` to stop; the database and Redis data remain in Docker volumes. `docker compose down -v` also deletes those volumes.

For a concise team handoff, see [TEAM_HANDBOOK.md](TEAM_HANDBOOK.md). Run `python3 package_for_team.py` to create a ZIP that excludes the local `.env` password.

## Current flow

`sample-pdfs/` → SHA-256 discovery → `documents` → dispatcher → per-attempt `extraction_sessions` → page-range `extraction_units` → extraction adapter → `extraction_raw_responses` → normalized metadata and voter tables → finalizer. The scheduler dispatches every 10–15 seconds and repairs stale/undelivered jobs every minute. Units use up to `MAX_UNIT_ATTEMPTS` automatic tries; a failed document can be manually retried as a new session. A failed unit in the current session can be manually retried without restarting completed units. Previous sessions and events remain available.

The UI includes summary counts, a searchable document list, document/session/unit pages, raw JSON, normalized records, validation errors, event history and manual retry controls. HTTP Basic authentication protects the monitor. It is intended for a trusted local/internal environment; add HTTPS and your organization's authentication before exposing it externally.

## Connect your extraction service

1. Set `EXTRACTOR_MODE=http`, `EXTRACTOR_URL` and (if needed) `EXTRACTOR_API_KEY` in `.env`.
2. Review **`app/extractor.py`** at the `TODO(integration)` marker. Its POST request currently sends:

   ```json
   {
     "document_id": "uuid",
     "document_location": "/data/pdfs/example.pdf",
     "page_from": 1,
     "page_to": 10,
     "schema_version": "electoral_v1"
   }
   ```

   Your service needs access to `document_location`. If it runs outside the Compose network, use a shared mount or adapt this method to upload PDF bytes/pages or pass an accessible URL. `host.docker.internal` in `.env.example` is just a starting URL, not an active service.
3. Return JSON like this; `POST /mock-extract` also serves this shape for contract testing:

   ```json
   {
     "extractor_version": "1.0.0",
     "page_from": 1,
     "page_to": 10,
     "document_metadata": {
       "hindi": {"state": "...", "district": "..."},
       "english": {"state": "...", "district": "..."},
       "common": {"roll_year": 2026}
     },
     "records": [{
       "source": {"page_number": 1, "row_number": 1},
       "hindi": {"name": "...", "relative_name": "...", "house_number": "...", "gender": "..."},
       "english": {"name": "...", "relative_name": "...", "house_number": "...", "gender": "..."},
       "common": {"serial_number": 1, "epic_number": "...", "age": 25, "relationship_type": "father"},
       "confidence": 0.96
     }]
   }
   ```

   Every record needs a page number within the requested range and a positive `row_number`. For failures, use `{"success":false,"error":{"code":"INVALID_PDF","message":"..."}}`. The client classifies `MODEL_TIMEOUT`, `EXTRACTION_FAILED`, `INTERNAL_ERROR` and `EXTRACTION_SERVICE_UNAVAILABLE` as retryable; other structured codes stop that unit until manual retry.
4. Review the `TODO(integration)` markers in **`app/normalize.py`** and add any final API fields to `app/models.py` and the mapping. Search with `rg 'TODO\(integration\)' app`. Raw JSON remains available even when normalization fails.
5. Existing tables are created by `app/init_db.py` on first start. `create_all` does not alter existing tables. Use a proper migration when you add columns to a database that already contains data.

## Document store adapter

The first store is a local folder mounted into web, worker and beat. Discovery identifies files by SHA-256, so the same bytes are registered once and changed bytes at the same filename become a new document. To connect another store, replace the local listing and file read at the `TODO(document-store)` points in `app/workflow.py`; keep stable source IDs where available. Ensure the worker and extraction service can access the selected document. Discovery only registers files and never extracts them.

## Tables

`documents`, `extraction_sessions`, `extraction_units`, `extraction_raw_responses`, `electoral_document_metadata`, `electoral_records`, and `processing_events` are defined in `app/models.py`. Hindi and English values use separate columns. `(session_id, page_number, source_row_number)` prevents duplicate normalized records. A raw response is committed before normalization; a retry reuses that raw response rather than invoking extraction again.

## Tests

Run `docker compose run --rm -v "$PWD/tests:/code/tests:ro" web python -m pytest -q tests` for contract and mapping tests. A full local smoke run can be checked by adding a PDF, clicking discovery, and viewing its session, units, raw responses and records in the UI. These mock records confirm pipeline mechanics only; extraction quality requires your service and real electoral PDFs.
