# Electoral-Roll Codebase Index

**Generated:** 2026-10-12  
**Branch:** aiml_backend  
**Latest Commit:** 01a99c0 (feat(ocr): anubhag name extraction from repeated headers with multi-page consensus and review reason consolidation)

This document provides a complete index of all source files, documentation, configuration, tests, and assets in the Electoral-Roll repository. It is intended as a reference for developers navigating the codebase.

---

## Table of Contents
1. [Core Configuration](#core-configuration)
2. [Documentation](#documentation)
3. [Source Code](#source-code)
4. [Tests](#tests)
5. [Scripts & Utilities](#scripts--utilities)
6. [Data & Artifacts](#data--artifacts)
7. [Requirements & Dependencies](#requirements--dependencies)
8. [Untracked / Generated Files](#untracked--generated-files)

---

## Core Configuration

| File | Purpose |
|------|---------|
| `.claude/settings.local.json` | Claude Code agent settings (local overrides) |
| `electoral-pipeline/docker-compose.yml` | Main Docker Compose definition for the full stack |
| `electoral-pipeline/docker-compose.linux.yml` | Linux-specific Compose overrides (if any) |

---

## Documentation

### Project-Level Docs (`/DOCS/`)
| File | Description |
|------|-------------|
| `GIT_SETUP.md` | Instructions for setting up Git credentials, SSH/HTTPS, and per-repo identity on shared machines |
| `info.md` | General project information and overview |
| `OCR_API_USAGE.md` | How to use the OCR API endpoints (upload, extract, review, etc.) |
| `OCR_TECHNICAL_STACK.md` | Detailed breakdown of technologies used (Python, FastAPI, Tesseract, PaddleOCR, MinIO, PostgreSQL, etc.) |
| `TEAM_HANDBOOK.md` | Team processes, coding standards, review workflow, and onboarding guide |

### Electoral-Pipeline Submodule Docs (`/electoral-pipeline/DOCS/`)
| File | Description |
|------|-------------|
| `info.md` | Submodule-specific overview |
| `OCR_API_USAGE.md` | OCR API usage details (duplicated for submodule context) |
| `OCR_TECHNICAL_STACK.md` | Technical stack details for the OCR pipeline |
| `TEAM_HANDBOOK.md` | Team handbook for the electoral-pipeline team |
| `TRANSLITERATION_ENGINE_ROUTING.md` | Details on the transliteration engine and routing logic for Indic scripts |

### OCR Subsystem Docs (`/electoral-pipeline/OCR/`)
| File | Description |
|------|-------------|
| `CLAUDE.md` | Claude Code instructions and agent definitions specific to OCR work |
| `ocr_api_requirements.txt` | Python package requirements for the OCR API services |
| `README.md` | Overview of the OCR subsystem, build/run instructions, and data flows |

### Electoral-Pipeline Root Docs (`/electoral-pipeline/docs/`)
| File | Description |
|------|-------------|
| `pdf-processing-stall-resolution-20261006.md` | Post-mortem/resolution notes for a PDF processing stall incident |
| `queue-diagnostic-20261006T112712.md` | Diagnostic notes on Celery queue behavior at a specific timestamp |

---

## Source Code

### Main Application (`/electoral-pipeline/app/`)
| File | Description |
|------|-------------|
| `archive_ingest.py` | Handles ingestion of ZIP archives from MinIO, extracts PDFs, and creates upload batches |
| `config.py` | Application configuration loading from environment variables and .env files |
| `config_store.py` | Utilities for persisting and retrieving configuration values in the database |
| `db_browse.py` | Database browsing utilities used by the `/db-browse` endpoint |
| `db.py` | SQLAlchemy database setup, session management, and base model definitions |
| `document_session.py` | Logic for managing user sessions during document review (pauses, resumes, etc.) |
| `document_store.py` | CRUD operations for extracted ElectoralRecord entities |
| `durations.py` | Helper functions for timing and measuring pipeline stage durations |
| `eci_source.py` | Integration with the Election Commission of India's public API (states endpoint) |
| `extraction_errors.py` | Definitions and handling of extraction/validation errors during OCR and parsing |
| `extractors.py` | Base extractor classes and specific implementations for different document types |
| `main.py` | FastAPI application entry point, route registration, middleware, and lifespan events |
| `models.py` | SQLAlchemy ORM models for Document, UploadBatch, ElectoralRecord, etc. |
| `ocr_adapter.py` | Adapter layer that normalizes responses from the two OCR services (pdf-api and text-api) |
| `official_sections.py` | Logic for matching extracted section names against the official ECI section list |
| `pipeline.py` | Core orchestration of the extraction pipeline (OCR → parsing → validation → storage) |
| `routes.py` | Definition of all HTTP routes (upload, extract, review, export, etc.) |
| `templates/__init__.py` | Makes the templates directory a Python package (enables template loading) |

### Templates (`/electoral-pipeline/app/templates/`)
| File | Description |
|------|-------------|
| `admin_users.html` | Page for managing admin users (list, create, delete) |
| `base.html` | Base template containing HTML structure, CSS/JS includes, Tailwind directives, and Jinja blocks |
| `batch_detail.html` | Detailed view of a single upload batch (progress, PDF rows, actions) |
| `browse_export_options.html` | Modal/form for configuring export options when browsing the database |
| `browse.html` | Main database browser interface (search, filter, pagination) |
| `dashboard.html` | Dashboard showing live statistics (record counts, pending reviews, etc.) |
| `document.html` | View for a single extracted document (shows fields, review actions) |
| `documents.html` | List/paginated view of all extracted documents |
| `export_options.html` | Standalone export options page (used from various contexts) |
| `folders.html` | Interface for managing folder-based uploads (similar to uploads.html) |
| `home.html` | Landing page (currently redirects to /upload; future work may make this a real landing page) |
| `layout.html` | Alternative layout wrapper (if used by specific routes) |
| `login.html` | Login form for admin access |
| `macros.html` | Reusable Jinja macros (buttons, badges, form fields, action menus) |
| `official_sections.html` | Page for viewing and searching the official ECI section list |
| `upload_detail.html` | Detail page for an upload batch (shows progress, rename form, extract/delete actions) |
| `uploads.html` | Main upload interface (select mode: PDF files, folder, or ZIP via MinIO) |
| `welcome.html` | Welcome page shown after login or on first visit |

### OCR Processing Services (`/electoral-pipeline/`)
| File | Description |
|------|-------------|
| `ocr_pdf_api.py` | **Modified**: OCR service for scanned PDFs (image-only runs). Uses Tesseract with multiple PSMs, preprocessing, majority voting, and now includes repeated-header consensus for anubhag name extraction. |
| `ocr_text_pdf_api.py` | OCR service for born-digital PDFs (text-layer). Extracts text directly via pdfminer/pymupdf and integrates with ECI states API for state validation. |

### Ground Truth Verification (`/electoral-pipeline/OCR/ground_truth_per_page_values_verified/`)
| File/Directory | Description |
|----------------|-------------|
| `whole_pdf_6-8_results.json` | Aggregated ground truth results for pages 6–8 of a reference PDF |
| `json_files/` | Per-page JSON ground truth results (page3_results.json through page21_results.json) |
| `json_files/baseline_backup/` | Backup copies of the above JSON files (for comparison) |
| `gt_text_files/` | Plain-text versions of the ground truth (for diffing) |
| `python_files/` | Python scripts used to generate/compare ground truth: <br> • `compare_gt.py` – main comparison script <br> • `run_page*.py` – runners that call OCR on a specific page and save results <br> • `compare_p*_gt.py` – per-page comparison helpers |
| `tests/` | Low-level OCR tests: <br> • `test_outer_line_removal.py` – tests removal of outer black borders <br> • `test_photo_mask.py` – tests application of photo masks to hide faces |

### Rolls Processing (`/electoral-pipeline/rolls/`)
| File | Description |
|------|-------------|
| `__init__.py` | Makes `rolls` a Python package |
| `pdf_processing_log.py` | Structured logging helpers specific to PDF processing (contextual info, timings) |

### Scripts (`/electoral-pipeline/scripts/`)
| File | Description |
|------|-------------|
| `backfill_session_durations.py` | Populates missing `session_duration` values in the database for past sessions |
| `check_ui_hindi_english.py` | Utility that scans extracted records and validates Hindi/English field population |
| `follow-pipeline-logs.sh` | Shell script that tails relevant log files (OCR, Celery, API) and highlights key events |
| `reconcile_celery_queue.py` | Checks for stuck tasks in Celery queues and optionally requeues or terminates them |
| `seed_page3.py` | Script that inserts a known-good page 3 PDF into the system for testing/regression |
| `start-ocr-api.sh` | Wrapper to launch the OCR API services (ocr_pdf_api and ocr_text_pdf_api) with correct environment |

### Migrations (`/electoral-pipeline/migrations/`)
| File | Description |
|------|-------------|
| `README.md` | Instructions on how to generate and apply database migrations (Alembic) |

---

## Tests (`/electoral-pipeline/tests/`)
| File | Description |
|------|-------------|
| `check_ui_hindi_english.py` | Test version of the Hindi/English validation script |
| `conftest.py` | Pytest configuration (fixtures, hooks, test database setup) |
| `seed_page3.py` | Test fixture to seed a known page 3 PDF |
| `test_admin_auth.py` | Tests for admin authentication routes and permissions |
| `test_anubhag_header.py` | **30 tests** for anubhag header parsing (`_anubhag_code_from_header_text`, `_split_anubhag_label`, and the new cleaning/selection functions) |
| `test_archive_ingest.py` | Tests for the ZIP ingestion pipeline (MinIO → PDF extraction → batch creation) |
| `test_confidence_coverage.py` | Tests that confidence thresholds and coverage metrics are computed correctly |
| `test_confidence_normalization.py` | Tests for confidence score normalization across different OCR engines |
| `test_contract.py` | Contract-style tests ensuring API request/response schemas match expectations |
| `test_db_browse_timezones.py` | Tests that timezone handling in the database browser is correct (IST ↔ UTC) |
| `test_document_session.py` | Tests for document session logic (pause/resume, timeout, concurrent users) |
| `test_document_store.py` | Tests for CRUD operations on extracted ElectoralRecord entities |
| `test_durations.py` | Tests for stage timing utilities |
| `test_eci_source.py` | Tests for the ECI states API wrapper (caching, error handling, fallback) |
| `test_extraction_errors.py` | Tests that extraction errors are properly categorized and propagated |
| `test_main.py` | Smoke tests for the main FastAPI application (healthcheck, route availability) |
| `test_models.py` | Unit tests for SQLAlchemy models (constraints, defaults, relationships) |
| `test_ocr_adapter.py` | Tests for the OCR adapter (normalization, fallback selection, error handling) |
| `test_official_sections.py` | Tests for official section matching (fuzzy logic, exact matches, conflict resolution) |
| `test_pipeline.py` | End-to-end tests of the extraction pipeline (using mocked OCR services) |
| `test_routes.py` | Tests for individual HTTP routes (request validation, response codes, DB effects) |
| `test_templates.py` | Tests that Jinja templates render without errors and contain expected blocks |

---

## Scripts & Utilities

### Root-Level Scripts
*(None currently; all scripts live under `/electoral-pipeline/scripts/`)*

---

## Data & Artifacts

### MinIO Data
- `/electoral-pipeline/minio-data/.minio.sys/format.json` – Internal MinIO metadata file (do not modify)

### Ground Truth (Verified)
Located under `/electoral-pipeline/OCR/ground_truth_per_page_values_verified/` as detailed above.

### Generated Reviews
- `/electoral-pipeline/reviews/anubhag_metadata_20261012/` – Output from a pipeline run that generated an HTML review (`index.html`), CSV (`section_metadata_review.csv`), and thumbnail images of PDF pages. **This directory is not tracked by Git** and is safe to delete/regenerate.

---

## Requirements & Dependencies

| File | Purpose |
|------|---------|
| `electoral-pipeline/OCR/ocr_api_requirements.txt` | Core Python dependencies for the OCR API services (FastAPI, Uvicorn, Pydantic, pdfminer.six, TesserOCR, etc.) |
| `electoral-pipeline/atl-llms.txt` | List of approved LLMs for Atlantic region processing (if applicable) |
| `electoral-pipeline/carbon-llms.txt` | List of approved LLMs for Carbon region processing |
| `electoral-pipeline/fluent-llms.txt` | List of approved LLMs for Fluent region processing |
| `electoral-pipeline/ocr_api_requirements.txt** (duplicate in root)** | Same as the OCR submodule copy – ensures visibility at repo root |
| `electoral-pipeline/design-qa.md** | Checklist and guidelines for design quality assurance (UI/UX, accessibility, etc.) |
| `electoral-pipeline/imperovements in speed and time.md** (note: likely typo in filename)** | Notes on performance improvements and timing optimizations |

---

## Untracked / Generated Files

These files are intentionally **not** version-controlled (they appear in `.gitignore` implicitly by directory patterns or are local-only):

- `**/__pycache__/` – Python bytecode caches
- `**/.pytest_cache/` – Pytest cache directory
- `**/.venv/` / `**/venv/` – Local Python virtual environments
- `**/dist/` / `**/build/` – Build artifacts (if any)
- `**/node_modules/` – JavaScript dependencies (if the project ever gains a frontend build step)
- `/electoral-pipeline/reviews/` – Generated HTML/CSV/image reviews from pipeline runs (safe to delete)
- `*.log` – Log files generated by services during runtime
- `.env` – Local environment variables (should **not** be committed; see `.env.example` if it exists)

---

## How to Use This Index

1. **Finding a file**: Use your editor’s “Go to File” feature and type part of the path or name.
2. **Understanding responsibilities**: Refer to the short descriptions above to know which layer owns a concern (e.g., OCR extraction vs. API routing vs. data persistence).
3. **Planning changes**: When touching a file, check if there are tests for it (usually a corresponding `test_*.py` file) and update/add tests as needed.
4. **Onboarding**: New team members should read `DOCS/GIT_SETUP.md`, `DOCS/TEAM_HANDBOOK.md`, and `electoral-pipeline/DOCS/OCR_TECHNICAL_STACK.md` before making changes.

---

## Notes

- The repository uses **FastAPI** for the web API, **Celery** for background jobs (OCR, ingest, export), **PostgreSQL** for data storage, and **MinIO** for object storage (PDF uploads, ZIP archives).
- The OCR pipeline runs two services in parallel: `ocr_pdf_api` (scanned/image PDFs) and `ocr_text_pdf_api` (born-digital/text PDFs). Results are deduplicated and merged by `ocr_adapter.py`.
- All database interactions go through SQLAlchemy ORM models defined in `app/models.py`.
- Configuration is centralized in `app/config.py` and loaded from environment variables at startup; secrets should never be hard-coded.
- The test suite is run with `pytest`; a `conftest.py` provides a temporary test database for each test session.

*This index was generated automatically from the current state of the repository. For the most up-to-date list, run:*  
```bash
find . -type f \( -name "*.py" -o -name "*.md" -o -name "*.html" -o -name "*.json" -o -name "*.yml" -o -name "*.yaml" -o -name "*.toml" -o -name "*.txt" -o -name "*.sh" -o -name "*.sql" -o -name "*.css" -o -name "*.js" -o -name "*.ts" \) \
  ! -path "*/node_modules/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/.pytest_cache/*" ! -path "*/dist/*" ! -path "*/build/*" ! -path "*/.venv/*" ! -path "*/venv/*" | sort
```