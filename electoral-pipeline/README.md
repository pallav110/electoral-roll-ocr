# Electoral PDF Processing Pipeline

This is a runnable first implementation of the team plan. PostgreSQL owns all document, session and unit state; Redis/Celery carries ID-only jobs. The included extraction adapter returns **synthetic sample voters** so you can exercise the whole pipeline before connecting your existing extraction service. No sample record is derived from the PDF.

## Start

1. Install Docker and start its engine (Docker Desktop on Windows, the `docker.io` package on Ubuntu).
2. From this directory, copy the env file and set a password:
   - Linux/macOS: `cp .env.example .env`
   - Windows PowerShell: `Copy-Item .env.example .env`
   - Windows cmd: `copy .env.example .env`

   Then edit `.env` and change `ADMIN_TOKEN`.
3. Start the stack — **pick the command for your OS**:

   ```bash
   # Windows
   docker compose up --build -d

   # Linux / Ubuntu  (adds the Linux path mapping; see below)
   docker compose -f docker-compose.yml -f docker-compose.linux.yml up --build -d
   ```

4. The included blank demo PDF can be used immediately, or add your own PDF files to `sample-pdfs/`.
5. Open [the local dashboard](http://localhost:8088), signing in as `admin` with your `ADMIN_TOKEN`. Use **Discover PDFs now** for an immediate scan. Celery Beat also scans hourly.

The dashboard listens on `127.0.0.1:8088` by default. Change `WEB_PORT` if occupied. Run `docker compose logs -f worker beat web` to follow processing. Run `docker compose down` to stop; the database and Redis data remain in Docker volumes. `docker compose down -v` also deletes those volumes.

For a concise team handoff, see [TEAM_HANDBOOK.md](TEAM_HANDBOOK.md). Run `python package_for_team.py` (`python3` on Linux) to create a ZIP that excludes the local `.env` password.

## Pointing at PDFs on either OS

In **Folders**, type a folder path and every PDF beneath it is discovered. The path is rewritten onto the mounted view of the host filesystem:

| | Windows host | Linux host |
|---|---|---|
| Typed | `C:\Users\me\Rolls` | `/srv/rolls` |
| Read from | `/host/c/Users/me/Rolls` | `/host/srv/rolls` |
| Also works | `D:\rolls` → `/host/d/rolls` | `/home/me/Desktop/x` → `/host/home/me/Desktop/x` |

Any absolute path works on both. On Linux that is because `docker-compose.linux.yml` mounts the host's filesystem root at `/host` (read-only). On Windows each drive is mounted separately and its letter becomes a path segment, which is how one mount root can stand for several drives.

The two deployments need different compose files because `//c/Users` — the UNC mount syntax — is Windows-only. On Linux Compose parses it as an ordinary relative path, and Docker creates the missing directory instead of failing, so the mount appears to succeed while staying empty. Use the two-file command on Linux; the plain `docker compose up` on Windows is unchanged and needs no override.

### Cloud-synced folders (OneDrive, Dropbox, Google Drive)

A folder of **Files On-Demand placeholders** can be listed but not read: the file appears in the directory with its full size, then fails on open with `Input/output error`. This is not a mount problem — the container has the path and the file is listed. It is the cloud provider refusing to materialise the bytes.

Such files are now reported instead of silently skipped. The folder row shows the count and names of what could not be read, so a scan that found nothing says *why*:

> `4 PDF(s) could not be read (cloud placeholders?): bill no 1.pdf, stamp.pdf, ...`

To make them readable, either pin the folder in File Explorer (right-click → Always keep on this device), or open the OneDrive client so it can fetch on demand. Files already on disk are unaffected.

### Scanning a very large folder

The walk is fully recursive with no depth limit, so pointing a root at a directory containing a `.venv`, `node_modules` or similar registers PDFs from inside it — correct, but slow over a bind mount. A root covering ~12,000 directories can take many minutes, and the request stays open the whole time. Prefer pointing roots at the folders that actually hold rolls.

To run the tests:

```bash
# Linux / macOS
docker compose run --rm -v "$PWD/tests:/code/tests:ro" web python -m pytest -q tests

# Windows PowerShell
docker compose run --rm -v "${PWD}/tests:/code/tests:ro" web python -m pytest -q tests
```

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
