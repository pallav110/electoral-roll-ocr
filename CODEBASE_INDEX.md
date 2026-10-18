    # CODEBASE INDEX — Electoral Roll OCR Platform
Generated: 2026-10-17 | Root: `c:\Users\pallav\Desktop\Python\OCR` | Branch: main (588ee5c)

Two top-level projects:

| Project | Path | Purpose |
|---|---|---|
| Electoral pipeline (runnable service) | `electoral-pipeline/` | FastAPI dashboard + Celery + 3 OCR micro-services + Postgres/Redis/MinIO via Compose |
| ECI downloader (sourcing tool) | `electoral-roll-automation-main/` | Playwright scraper + FastAPI API downloading roll PDFs from voters.eci.gov.in |
| Shared docs | `DOCS/`, `electoral-pipeline/DOCS/` | API usage, tech stack, transliteration routing, handbook |
| CI | `.github/workflows/tests.yml` | app-tests + ocr-tests (docker) + lint |

## 1. Docker Compose — `electoral-pipeline/docker-compose.yml` (+ `docker-compose.linux.yml`)
17 services. Windows `x-host-mounts`: `./app`, `${PDF_SOURCE_PATH:-./sample-pdfs}:/data/pdfs`, `//c/Users:/host/c/Users`, `//d:/host/d`, `./ingest:/data/ingest`. Linux override: `/:/host:ro`, `HOST_MOUNT_STYLE=posix`.

| Service | Build/Image | Ports | Role |
|---|---|---|---|
| minio | Dockerfile.minio (MinIO 2025-10-15 built from source) | 9000 S3, 9001 console | buckets electoral-archives + electoral-derived-assets; vol minio-data:/data |
| db | postgres:16-alpine | 127.0.0.1:5433->5432 | all state; vol pgdata |
| redis | redis:7-alpine | internal | Celery broker+backend; vol redisdata |
| ocr | Dockerfile.ocr -> ocr_pdf_api.py | 127.0.0.1:8082 | scanned-image OCR: Tesseract 5.5.3/leptonica 1.87.0 source-built + PaddleOCR 2.10 CPU + tesserocr 2.11 |
| ocr-text | Dockerfile.ocr.text -> ocr_text_pdf_api.py | 8083 | born-digital text-layer reader, no Paddle; 422 on scanned PDFs |
| image-extractor | Dockerfile.images -> ocr_image_and_details_extractor.py | 8084 + 8095 maps | gallery + map evidence/georef |
| osm-geocoder | mediagis/nominatim:5.3 | 8080 internal | local India OSM DB for cover locations |
| init | build:. app/init_db.py | - | create_all + patches; gate for web/workers/beat |
| web | Dockerfile uvicorn app.main:app:8000 | 8088 host | dashboard + uploads + browse/export + MinIO proxy + SigNoz SSO |
| worker-control | build:. -Q control -c1 | - | fast DB bookkeeping |
| worker-ingest | build:. -Q ingest -c1 | - | MinIO ZIP ingest |
| worker-cover | build:. -Q cover -c1 | - | cover reads 5-10s each |
| worker-ocr | build:. -Q ocr -c1 | - | process_unit voter OCR |
| worker-images | build:. -Q images -c4 | - | gallery backfill |
| worker-locations | worker-control img -Q locations -c2 | - | OSM location resolve |
| worker-map-verify | worker-control img -Q map-verify -c1 | - | map-image verification |
| beat | build:. celery beat | - | schedules (see S3) |

Volumes: pgdata, redisdata, ocr-results (/results), ocr-paddle-cache, ocr-text-results, image-extractor-results, osm-geocoder-data, osm-geocoder-flatnode.

## 2. FastAPI services (5 apps)
- app/main.py dashboard: 60+ routes, Depends(admin) except /health. Auth GET/POST /login, POST /logout, admin/users, api/admin/users, api/me, /signoz, /health. Pages / dashboard /documents /sessions /units /folders + POST /actions/discover. Uploads GET /upload POST /upload /uploads + zip/{start,part,complete,abort} + per-doc/batch ops. Ops retry/extract/cancel + POST /mock-extract. Browse GET /browse rows export + jobs. Templates: 19 Jinja2 in app/templates/.
- ocr_pdf_api.py ~7500 lines: Tess hin+eng 200DPI + Paddle en; 3x10 lattice 30 slots; pool + serial Paddle + asyncio lock; deleted 8-reader 5-hit vote. GET /health /ocr/progress; POST /ocr/extract (pdf_file,start_page,end_page,whole_pdf,skip_non_voter_pages); POST /ocr/cover.
- ocr_text_pdf_api.py ~1900 lines (imports ocr_pdf_api layer): GET /health /ocr/progress; POST /ocr/extract 422-on-scanned. Router: app/extractor._resolve_ocr_url, OCR_ROUTER=on.
- ocr_image_and_details_extractor.py + map_georeference.py: GET /health; POST /image-details/extract|georeference|map-*; GET /image-details/asset*. ocr_cover.py backs app/cover.py.
- downloader src/api.py: GET /health /states .../years .../roll-types .../districts .../constituencies .../languages; POST /download; GET /download/log /download/zip/{state} /download/zip/all.


## 3. Celery (app/tasks.py + app/workflow.py)
- Celery electoral, redis broker+backend, JSON, acks_late, prefetch 1. Queues: control,ingest,ocr,cover,images,locations,map-verify (default control). 14 tasks: discover_documents, ingest_minio_zip_archives, dispatch_pending_documents/units, recover_jobs, sweep_covers, dispatch_visual_images/locations, read_document_cover, extract_document_images, resolve_document_location, verify_map_image, process_document, process_unit.
- Beat: discover 3600s, ingest-archives 30s, dispatch-docs 15s, dispatch-units 10s, recover 60s, sweep-covers 60s, dispatch-visual 15s, dispatch-locations 30s. PAGES_PER_UNIT 10 (beat 3), MAX_ATTEMPTS 3, RETRY 30*2^n, STALE 600, OCR_INFLIGHT 2, DOC_INFLIGHT 20, visibility 2*timeout+3600. Raw committed before normalize; retry reuses raw.

## 4. MinIO / storage
- Pipeline app/archive_ingest.py (boto3) polls electoral-archives/incoming/ 30s -> atomic ZIP extract -> batches. app/minio_proxy.py console SSO proxy. app/uploads.py atomic save. refuse_bad_minio_config: in-net http://minio:9000 + :9001 console, public via /minio proxy. Buckets electoral-archives + electoral-derived-assets. Upload caps 100MB/file 2000MB/batch 100/batch-display. PDF_SOURCE_PATH /data/pdfs, INGEST_ROOT /data/ingest, DOCUMENT_ROOT sample-pdfs.
- Downloader src/minio_client.py uploads downloaded_pdfs + electoral_rolls_* zips (docs/MINIO.md).

## 5. Postgres (app/models.py, 16 tables)
- admin_users; documents (pending/dispatched/queued/processing/completed/retry/failed/cancelled/paused; uq source/hash); document_visual_extractions/assets; document_locations; document_map_verifications; upload_batches; scan_roots; archive_ingestions; extraction_sessions/units; extraction_raw_responses (raw JSON); electoral_document_metadata; electoral_records ((session,page,row) unique; hi/en split; voter_sur_name; voter_sr_no str; sno per-request int; is_deleted bool); document_covers; processing_events. init_db + migrate.py + schema_patch.py.

## 6. app/ modules (48 files)
- Core: main, config (env + MinIO/host-mount validation), db, models, tasks, workflow (discover/dispatch/process/recover, local_source_id scope:hash, _resolve_scan_path drive|posix).
- Extract: extractor (Http/mock + _resolve_ocr_url + validate_response + ExtractionError), normalize (map_record/map_metadata), ocr_adapter (label-anchored fallback), cover, header_fill, roll_gate, roll_template (+roll_templates/).
- Views: record_scope, presentation, ui_status, workflow_progress, durations, browse_export, db_browse.
- Infra: humanlog/logging_config/pipeline_logging, api_auth, manage_users, signoz_sso, uploads, archive_ingest, document_store/locations/session, persist, purge, migrate/init_db/schema_patch/config_store.
- Lang: transliterate + name_translation + name_lexicon.json (indic-transliteration 2.3.82), official_sections.
- Geo/visual: map_verification + map_verify_dispatch, visual_assets, eci_source, extraction_errors.

## 7. Config (electoral-pipeline/.env.example)
- DATABASE_URL db:5432, REDIS_URL, DOCUMENT_ROOT, PDF_SOURCE_PATH, MINIO_* (http://minio:9000, :9001 console, buckets/prefix/region), EXTRACTOR_MODE=http, EXTRACTOR_URL http://ocr:8082/ocr/extract, TEXT_EXTRACTOR_URL http://ocr-text:8083/ocr/extract, OCR_ROUTER=on, TIMEOUT 120 (compose 5400), PAGES_PER_UNIT, ADMIN_TOKEN, SESSION_SECRET 32+, SIGNOZ_*, WEB_PORT 8088, HOST_MOUNT_*. Never commit .env.

## 8. Tests + CI + scripts/tools
- tests/ (44 files): contract/scheme/ocr_routing/presentation; OCR pins age_ink_guard, house_number/age_recovery, review_triggers (flag!=rewrite), header_geometry (32), hindi_name_split, outer_line_removal, tesseract_grouping, singh_glyph, confidence_*; pipeline queue_routing/windows, live/fused_progress, ocr_progress_counter, document_recovery, duration_honesty, upload*/batches/layout/regressions, archive_ingest, normalize_unit_db, record_scope, relation_pair, roll_year, page_attribution, scan_paths, official_sections, templates_compile, transliterate/lexicon, package_for_team, error_classification, db_browse_timezones, admin_auth; conftest, seed_page3; test_scripts/ 58 lansdown probes + ocr_first_six, trace_card_pipeline, render_flagged_cards 400DPI. pyproject testpaths=[tests], markers slow/ocr.
- Tools: tools/corpus_sweep.py (161-roll), backfill_{combined,age,gender}, import_official_sections; scripts/ start-ocr-api.sh, follow-pipeline-logs.sh, reconcile_celery_queue.py, seed_page3.py, check_ui_hindi_english.py; OCR/ mirror (input/scripts/tests); signoz/ otel stack; sample-pdfs/ demo-blank-13 + S24-53; rolls/, ground_truth/, ingest/, vendor/ empty.
- Downloader: scripts/ 30+ probes (captcha/submit/parts_ajax/live/real_download), tests/, debug/, run_test.py.
- CI tests.yml: app-tests (requirements.txt + cv2/numpy/fitz/pytesseract + ruff + pytest), ocr-tests (compose build ocr + pytest --ignore=test_scripts + invariants 30 lattice / 8 readers / 5-hit bar), lint ruff.

## 9. Key docs
- README.md (99.7% field / 96.7% whole-record on 90 GT; 11/531 flagged 2.1%); CLAUDE.md (200DPI both engines, lattice-not-count, _PADDLE_LOCK singleton-only, majority serial fix, _DEVA_RUN classifier); pipeline README (cp .env.example .env -> compose up --build -d -> :8088 Discover); DOCS/OCR_API_USAGE.md (handoff + 41-43s/page, 818.8s/22pp/531rec); DOCS/OCR_TECHNICAL_STACK.md (Tess-hi vs Paddle-meta + geometry fix); TRANSLITERATION_ENGINE_ROUTING.md; TEAM_HANDBOOK.md x2; PATCH_A_ocr_progress_endpoint.md; DOCS/docs/codebase-index.md (older auto-index, superseded here).

