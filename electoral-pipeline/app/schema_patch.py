"""Additive PostgreSQL columns for deployments that already ran create_all."""
from sqlalchemy import text

from app.db import engine


def _run(sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql))


def apply_schema_patches() -> None:
    """Idempotent ALTER TABLE for new business fields (Django model parity)."""
    patches = [
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS booth_no VARCHAR(64) DEFAULT ''",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS extraction_config_id UUID REFERENCES extraction_configs(id)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_job_id UUID REFERENCES processing_jobs(id)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_text_based BOOLEAN",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS used_ocr BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS extraction_config_id UUID REFERENCES extraction_configs(id)",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS booth_no VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS state_code VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS ac_code VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS district VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS booth_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS anubhag_code VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS anubhag_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS start_serial_no VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS end_serial_no VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS male VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS female VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS third_gender VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS total VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS gali_no JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS raw_text TEXT DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS page_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS pdf_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS data JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS data_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_document_metadata ADD COLUMN IF NOT EXISTS record_status VARCHAR(20) DEFAULT 'pending'",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_sr_no VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS serial_mark VARCHAR(16)",
        "ALTER TABLE electoral_records ALTER COLUMN serial_mark TYPE VARCHAR(16)",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS gender VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS house_no VARCHAR(128) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_first_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_middle_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_sur_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS relation_name VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_husband_first_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_husband_middle_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_husband_last_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_father_first_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_father_middle_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_father_last_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_mother_first_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_mother_middle_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_mother_last_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_other_first_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_other_middle_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS voter_other_last_name VARCHAR(255) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS state_code VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS ac_code VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS anubhag_code VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS anubhag_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS ward_code VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS roll_format VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS anubhag_status VARCHAR(32) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS booth_code VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS booth_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS booth_no VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS pdf_name VARCHAR(512) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS record_data JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS record_status VARCHAR(20) DEFAULT 'pending'",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS data_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS raw_text TEXT DEFAULT ''",
        "ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS page_name VARCHAR(512) DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_records_voter_sr ON electoral_records (voter_sr_no)",
        "CREATE INDEX IF NOT EXISTS idx_records_state_ac_booth ON electoral_records (state_code, ac_code, booth_code)",
        "CREATE INDEX IF NOT EXISTS idx_records_pdf_name ON electoral_records (pdf_name)",
        "CREATE INDEX IF NOT EXISTS idx_documents_config ON documents (extraction_config_id)",
        "CREATE INDEX IF NOT EXISTS idx_documents_job ON documents (processing_job_id)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON processing_jobs (status)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_created ON processing_jobs (created_at DESC)",
    ]
    for statement in patches:
        _run(statement)
