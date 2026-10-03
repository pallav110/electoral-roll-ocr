import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def now():
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    current_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    sessions: Mapped[list["ExtractionSession"]] = relationship(back_populates="document", order_by="ExtractionSession.attempt_number.desc()")
    __table_args__ = (
        CheckConstraint("status IN ('pending','dispatched','queued','processing','completed','retry','failed','cancelled')", name="documents_status"),
        Index("idx_documents_status", "status"),
        Index("idx_documents_pending", "priority", "discovered_at", postgresql_where=text("status IN ('pending','retry')")),
        Index("uq_documents_source", "source_type", "source_document_id", unique=True, postgresql_where=text("source_document_id IS NOT NULL")),
        Index("uq_documents_hash", "source_type", "file_hash", unique=True, postgresql_where=text("source_document_id IS NULL AND file_hash IS NOT NULL")),
    )


class ScanRoot(Base):
    """A folder the pipeline walks looking for PDFs.

    Deliberately separate from `Document`. A document is a file that was
    found; a scan root is a place to look. Keeping them apart means the same
    PDF can be reached through more than one root without either table
    having to model the other, and adding a root never rewrites history.

    `path` is stored exactly as typed, and is resolved inside the container
    that walks it -- not on the host and not at import time. See
    app/workflow.py::discover for why that matters.
    """
    __tablename__ = "scan_roots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Set by the last discovery pass, so the UI can show what a root is
    # actually finding rather than only what the user believes it holds.
    last_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class ExtractionSession(Base):
    __tablename__ = "extraction_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="created", nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    extractor_version: Mapped[str | None] = mapped_column(String(100))
    extraction_schema_version: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages_total: Mapped[int | None] = mapped_column(Integer)
    pages_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_extracted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_time_ms: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    document: Mapped[Document] = relationship(back_populates="sessions")
    units: Mapped[list["ExtractionUnit"]] = relationship(back_populates="session", order_by="ExtractionUnit.unit_number")
    __table_args__ = (UniqueConstraint("document_id", "attempt_number"), CheckConstraint("status IN ('created','queued','processing','completed','partial','failed','abandoned')", name="sessions_status"))


class ExtractionUnit(Base):
    __tablename__ = "extraction_units"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_sessions.id"), nullable=False)
    unit_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_from: Mapped[int] = mapped_column(Integer, nullable=False)
    page_to: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_extracted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    session: Mapped[ExtractionSession] = relationship(back_populates="units")
    __table_args__ = (UniqueConstraint("session_id", "unit_number"), CheckConstraint("status IN ('pending','queued','processing','completed','retry','failed')", name="units_status"), Index("idx_units_dispatch", "status", "next_retry_at"))


class RawResponse(Base):
    __tablename__ = "extraction_raw_responses"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_sessions.id"), nullable=False)
    extraction_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_units.id"))
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    extractor_version: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (Index("idx_raw_unit", "extraction_unit_id"),)


class ElectoralDocumentMetadata(Base):
    __tablename__ = "electoral_document_metadata"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_sessions.id"), nullable=False)
    state_hi: Mapped[str | None] = mapped_column(Text)
    state_en: Mapped[str | None] = mapped_column(Text)
    district_hi: Mapped[str | None] = mapped_column(Text)
    district_en: Mapped[str | None] = mapped_column(Text)
    assembly_constituency_number: Mapped[int | None] = mapped_column(Integer)
    assembly_constituency_hi: Mapped[str | None] = mapped_column(Text)
    assembly_constituency_en: Mapped[str | None] = mapped_column(Text)
    part_number: Mapped[int | None] = mapped_column(Integer)
    polling_station_hi: Mapped[str | None] = mapped_column(Text)
    polling_station_en: Mapped[str | None] = mapped_column(Text)
    polling_station_address_hi: Mapped[str | None] = mapped_column(Text)
    polling_station_address_en: Mapped[str | None] = mapped_column(Text)
    roll_year: Mapped[int | None] = mapped_column(Integer)
    publication_date: Mapped[datetime | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (UniqueConstraint("session_id", name="uq_metadata_session"),)


class ElectoralRecord(Base):
    __tablename__ = "electoral_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_sessions.id"), nullable=False)
    extraction_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_units.id"))
    page_number: Mapped[int | None] = mapped_column(Integer)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    serial_number: Mapped[int | None] = mapped_column(Integer)
    epic_number: Mapped[str | None] = mapped_column(String(50))

    # OCR metadata fields
    sno: Mapped[int | None] = mapped_column(Integer)
    card_index: Mapped[int | None] = mapped_column(Integer)
    voter_sr_no: Mapped[str | None] = mapped_column(String(50))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    state_code: Mapped[str | None] = mapped_column(String(10))
    ac_code: Mapped[str | None] = mapped_column(String(10))
    anubhag_code: Mapped[int | None] = mapped_column(Integer)
    anubhag_name: Mapped[str | None] = mapped_column(Text)
    booth_code: Mapped[str | None] = mapped_column(String(10))
    pdf_name: Mapped[str | None] = mapped_column(Text)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_reasons: Mapped[list | None] = mapped_column(JSONB)
    field_sources: Mapped[dict | None] = mapped_column(JSONB)

    # Voter name components (Hindi + English)
    name_hi: Mapped[str | None] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    voter_first_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_first_name_en: Mapped[str | None] = mapped_column(Text)
    voter_middle_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_middle_name_en: Mapped[str | None] = mapped_column(Text)
    voter_sur_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_sur_name_en: Mapped[str | None] = mapped_column(Text)

    # Relative name components (Hindi + English)
    relative_name_hi: Mapped[str | None] = mapped_column(Text)
    relative_name_en: Mapped[str | None] = mapped_column(Text)
    relation_name: Mapped[str | None] = mapped_column(String(50))  # पिता, पति, माता, अन्य

    # Father's name components
    voter_father_first_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_father_first_name_en: Mapped[str | None] = mapped_column(Text)
    voter_father_middle_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_father_middle_name_en: Mapped[str | None] = mapped_column(Text)
    voter_father_last_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_father_last_name_en: Mapped[str | None] = mapped_column(Text)

    # Husband's name components
    voter_husband_first_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_husband_first_name_en: Mapped[str | None] = mapped_column(Text)
    voter_husband_middle_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_husband_middle_name_en: Mapped[str | None] = mapped_column(Text)
    voter_husband_last_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_husband_last_name_en: Mapped[str | None] = mapped_column(Text)

    # Mother's name components
    voter_mother_first_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_mother_first_name_en: Mapped[str | None] = mapped_column(Text)
    voter_mother_middle_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_mother_middle_name_en: Mapped[str | None] = mapped_column(Text)
    voter_mother_last_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_mother_last_name_en: Mapped[str | None] = mapped_column(Text)

    # Other relative's name components
    voter_other_first_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_other_first_name_en: Mapped[str | None] = mapped_column(Text)
    voter_other_middle_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_other_middle_name_en: Mapped[str | None] = mapped_column(Text)
    voter_other_last_name_hi: Mapped[str | None] = mapped_column(Text)
    voter_other_last_name_en: Mapped[str | None] = mapped_column(Text)

    # Relationship type (English normalized)
    relationship_type: Mapped[str | None] = mapped_column(String(30))
    age: Mapped[int | None] = mapped_column(Integer)
    gender_hi: Mapped[str | None] = mapped_column(String(50))
    gender_en: Mapped[str | None] = mapped_column(String(50))
    house_number_hi: Mapped[str | None] = mapped_column(Text)
    house_number_en: Mapped[str | None] = mapped_column(Text)
    section_number: Mapped[int | None] = mapped_column(Integer)
    section_name_hi: Mapped[str | None] = mapped_column(Text)
    section_name_en: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    source_record_hash: Mapped[str | None] = mapped_column(String(64))
    # SHA-256 of the Hindi values the *_en columns were derived from. If a
    # later OCR pass corrects the Hindi, this no longer matches and the
    # English value is stale and must be regenerated.
    translit_source_hash: Mapped[str | None] = mapped_column(String(64))
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validation_errors: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (
        # The natural key of an extracted voter: which attempt produced it, which
        # page it was printed on, which card on that page. The unique constraint
        # on it makes re-normalising the same unit idempotent.
        #
        # The partial WHERE matters: a card position can be NULL when the row
        # comes from a partial tail page, so without it two NULL-bearing rows
        # would not conflict with each other and a re-run would duplicate them.
        Index(
            "uq_electoral_record_source",
            "session_id",
            "page_number",
            "source_row_number",
            unique=True,
            postgresql_where=text("page_number IS NOT NULL AND source_row_number IS NOT NULL"),
        ),
        Index("idx_records_epic", "epic_number"),
        Index("idx_records_name_en", "name_en"),
        Index("idx_records_unit", "extraction_unit_id"),
        # Covers the shape of the three record queries that dominate this app:
        # "records for this document", ordered by page then row. Without it each
        # of them sequential-scans the whole table -- the largest one in the
        # schema -- and then sorts the result.
        #
        # (document_id, page_number) rather than (document_id, page_number,
        # source_row_number): the leading two columns are what every one of those
        # queries filters and orders on, and including the third would stop
        # PostgreSQL from using this index to satisfy an ORDER BY that omits it.
        Index("idx_records_document", "document_id", "page_number"),
    )


class ProcessingEvent(Base):
    __tablename__ = "processing_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_sessions.id"))
    extraction_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_units.id"))
    service: Mapped[str | None] = mapped_column(String(100))
    event_type: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (Index("idx_events_document", "document_id", "created_at"), Index("idx_events_unit", "extraction_unit_id", "created_at"))
