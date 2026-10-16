# Database migrations

This service uses **SQLAlchemy** (not Django). Schema changes are applied by:

1. `python -m app.migrate` — drops legacy `documents` / `electoral_*` tables (destructive).
2. `python -m app.init_db` — runs the migration step above, then `create_all` for the current models.

The table layout matches the `pdf_processor` Django models (`pdf_processor_*` tables) plus `extraction_sessions`, `extraction_units`, and `extraction_raw_responses` for the existing 4-worker Celery orchestration.
