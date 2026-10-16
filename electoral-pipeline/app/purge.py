"""Remove all pipeline run data so discovery can start fresh."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.logging_config import pipeline_step

# Extraction config rows are kept; everything else is processing output and state.
_PURGE_TABLES = (
    "extraction_raw_responses",
    "extraction_units",
    "extraction_sessions",
    "pdf_processor_extractedrecord",
    "pdf_processor_processingerror",
    "pdf_processor_pdfdocument",
    "pdf_processor_processingjob",
    "pdf_processor_extractedvoter",
    "pdf_processor_pdfscanstatus",
)


def purge_all_pipeline_data(db: Session) -> None:
    tables = ", ".join(_PURGE_TABLES)
    db.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY"))
    db.commit()
    pipeline_step("UI: purged all pipeline data (%s tables)", len(_PURGE_TABLES))


def purge_celery_queue() -> int:
    """Drop pending Celery tasks so workers do not touch deleted rows."""
    try:
        from app.tasks import celery_app
    except ImportError:
        return 0
    return celery_app.control.purge() or 0
