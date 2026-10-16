"""Active extraction configuration (PDF folder path)."""
from sqlalchemy import select

from app import config
from app.db import SessionLocal
from app.models import ExtractionConfig, ProcessingJob, JobStatus, now


def get_or_create_default_config(db) -> ExtractionConfig:
    folder = str(config.DOCUMENT_ROOT)
    row = db.scalar(select(ExtractionConfig).where(ExtractionConfig.is_active.is_(True)).limit(1))
    if row:
        if row.pdf_folder_path != folder:
            row.pdf_folder_path = folder
        return row
    row = db.scalar(select(ExtractionConfig).where(ExtractionConfig.name == "default").limit(1))
    if row:
        row.pdf_folder_path = folder
        row.is_active = True
        return row
    row = ExtractionConfig(
        name="default",
        pdf_folder_path=folder,
        page_number=1,
        is_active=True,
        extraction_rules={},
    )
    db.add(row)
    db.flush()
    return row


def get_or_create_active_job(db, extraction_config: ExtractionConfig) -> ProcessingJob:
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.config_id == extraction_config.id, ProcessingJob.status == JobStatus.RUNNING.value)
        .order_by(ProcessingJob.id.desc())
        .limit(1)
    )
    if job:
        job.last_activity_at = now()
        return job
    job = ProcessingJob(config_id=extraction_config.id, status=JobStatus.RUNNING.value, started_at=now(), last_activity_at=now())
    db.add(job)
    db.flush()
    return job


def ensure_default_config() -> ExtractionConfig:
    with SessionLocal.begin() as db:
        return get_or_create_default_config(db)
