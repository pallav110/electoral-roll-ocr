import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def now():
    return datetime.now(timezone.utc)


class AdminUser(Base):
    """Human accounts permitted to use the internal voter-data application.

    Passwords are stored solely as salted scrypt hashes.  Service credentials
    such as the OCR worker API key do not use this table.
    """
    __tablename__ = "admin_users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # Which registered folder this file was found under. Deliberately NOT
    # derived from source_path at read time: for a path that is not bind-
    # mounted, discovery copies the PDFs into the ingest folder and scans
    # them from there, so source_path then points inside the container and
    # has no prefix relationship to the path the user typed. Storing the
    # link is what makes a folder view exact rather than approximately
    # right. NULL on rows discovered before this column existed, which is
    # why folder queries treat NULL as "belongs to no folder" instead of
    # dropping the row.
    scan_root_id: Mapped[int | None] = mapped_column(ForeignKey("scan_roots.id"))
    # The upload this file arrived in, and its position inside it.
    #
    # UploadBatch is the user-facing grouping -- one row per upload, with the
    # PDFs underneath split into fixed-size batches. Both columns are NULL for
    # anything discovered by other means (scan folders, MinIO archives picked
    # up before this existed), and every query that reads them treats NULL as
    # "belongs to no upload" rather than dropping the row. Ordering by
    # source_path within a batch is what makes the numbering stable, so the
    # same file always lands in the same position on a re-scan.
    upload_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("upload_batches.id"))
    batch_index: Mapped[int | None] = mapped_column(Integer)
    # Has the user asked for this document to be processed?
    #
    # This is the gate that makes /uploads a staging area rather than a
    # fire button. dispatch_documents() runs every 15 seconds off the beat
    # schedule and picks up every pending row, so removing the discovery call
    # from the upload route is NOT enough on its own -- without this column
    # an upload would still start OCR within a beat tick of landing on disk,
    # and the user would never get the chance to notice a wrong file first.
    # Only ticked rows are visible to the dispatcher.
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    current_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When this document was last handed to the broker, and by whom.
    #
    # recover() reads this to decide whether a "queued" document whose session has
    # done no work is a lost publish rather than one still in flight. dispatch_documents()
    # sets it on publish and recover() refreshes it on each republish, so the
    # column is a real deadline rather than the NULL it always was -- the branch
    # was previously unreachable and a document whose message the broker dropped
    # stayed "queued" forever.
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
    upload_batch: Mapped["UploadBatch | None"] = relationship(back_populates="documents")
    __table_args__ = (
        CheckConstraint("status IN ('pending','dispatched','queued','processing','completed','retry','failed','cancelled','paused')", name="documents_status"),
        Index("idx_documents_status", "status"),
        Index("idx_documents_scan_root", "scan_root_id"),
        Index("idx_documents_batch", "upload_batch_id", "batch_index"),
        Index("idx_documents_pending", "priority", "discovered_at", postgresql_where=text("status IN ('pending','retry')")),
        Index("uq_documents_source", "source_type", "source_document_id", unique=True, postgresql_where=text("source_document_id IS NOT NULL")),
        Index("uq_documents_hash", "source_type", "file_hash", unique=True, postgresql_where=text("source_document_id IS NULL AND file_hash IS NOT NULL")),
    )


class DocumentVisualExtraction(Base):
    """Cached visual-page extraction for one immutable source PDF."""
    __tablename__ = "document_visual_extractions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("document_id", "source_hash", name="uq_visual_extraction_document_source"),
        CheckConstraint("status IN ('pending','processing','completed','failed')", name="visual_extractions_status"),
    )


class DocumentVisualAsset(Base):
    """Metadata for a panel image stored in MinIO, never the image bytes."""
    __tablename__ = "document_visual_assets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_visual_extractions.id", ondelete="CASCADE"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    image_number: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text)
    image_text: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (UniqueConstraint("extraction_id", "page_number", "image_number", name="uq_visual_asset_position"),)


class DocumentLocation(Base):
    """One auditable polling-station location per source PDF.

    A coordinate belongs to the polling-station metadata on the cover, not to
    every voter card and not to a cropped map image.  Keeping it separately
    avoids duplicating a potentially unverified coordinate across hundreds of
    voter rows and leaves a clear audit trail for every map button.
    """
    __tablename__ = "document_locations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    matched_name: Mapped[str | None] = mapped_column(Text)
    matched_address: Mapped[str | None] = mapped_column(Text)
    provider_payload: Mapped[dict | None] = mapped_column(JSONB)
    verification_score: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_location_document"),
        CheckConstraint("status IN ('pending','processing','verified','unverified','failed','skipped')", name="document_locations_status"),
        Index("idx_document_locations_status", "status"),
    )


class DocumentMapVerification(Base):
    """Measured georeference of one Google-map image.

    Cover-page polling-station search results live on document_locations and
    must never be copied here. Latitude and longitude stay null unless the
    landmark fit passed the residual and coverage gates.
    """
    __tablename__ = "document_map_verifications"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    visual_asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_visual_assets.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    source_landmarks: Mapped[list | None] = mapped_column(JSONB)
    pin_pixel: Mapped[dict | None] = mapped_column(JSONB)
    transformation_metrics: Mapped[dict | None] = mapped_column(JSONB)
    rmse_meters: Mapped[float | None] = mapped_column(Numeric(8, 1))
    max_residual_meters: Mapped[float | None] = mapped_column(Numeric(8, 1))
    method: Mapped[str | None] = mapped_column(String(64))
    error_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("visual_asset_id", name="uq_map_verification_asset"),
        CheckConstraint(
            "status IN ('pending','processing','verified','unverified','failed')",
            name="document_map_verifications_status",
        ),
        Index("idx_map_verifications_status", "status"),
    )


class UploadBatch(Base):
    """One upload: the parent grouping on /uploads.

    A batch is "one main / underneath it batches" -- the user uploads a folder
    of 300 PDFs and gets back a single row here whose documents are numbered
    1..N, which the page then renders as fixed-size groups of
    config.UPLOAD_BATCH_SIZE.

    Kept separate from ScanRoot on purpose. A scan root is a place the
    pipeline looks repeatedly; an upload batch is a one-shot submission the
    user wants to see, tick, and run. Folding the second into the first would
    mean a batch could be deleted out from under documents still being read.

    `file_count` is the number of PDFs the upload claimed, not the number
    that made it into the database. The two differ when a duplicate is
    rejected by uq_documents_hash, and the user asked specifically to be able
    to see what arrived -- so the claimed number is what the page shows.
    """
    __tablename__ = "upload_batches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # files | folder | minio. How the PDFs arrived, not what happens next.
    source: Mapped[str] = mapped_column(String(20), default="files", nullable=False)
    # The directory under DOCUMENT_ROOT holding the files, when there is one.
    # NULL for MinIO archives until the beat extracts them to disk.
    relative_directory: Mapped[str | None] = mapped_column(Text)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    documents: Mapped[list["Document"]] = relationship(back_populates="upload_batch")
    __table_args__ = (
        Index("idx_upload_batches_created", "created_at"),
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

class ArchiveIngestion(Base):
    __tablename__ = "archive_ingestions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str] = mapped_column(String(128), nullable=False)
    object_size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    pdf_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extracted_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_directory: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("bucket", "object_key", "etag", name="uq_archive_ingestion_object_version"),
        CheckConstraint("status IN ('pending','processing','completed','failed')", name="archive_ingestions_status"),
        Index("idx_archive_ingestions_status", "status"),
    )


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
    # The small box printed beside the serial on an addition-list card.
    # A normal card has no such box; those rows store "Not printed" so the
    # cell does not look like a value the reader missed.
    serial_mark: Mapped[str | None] = mapped_column(String(16))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    state_code: Mapped[str | None] = mapped_column(String(10))
    ac_code: Mapped[str | None] = mapped_column(String(10))
    anubhag_code: Mapped[int | None] = mapped_column(Integer)
    anubhag_name: Mapped[str | None] = mapped_column(Text)
    ward_code: Mapped[str | None] = mapped_column(String(32))
    roll_format: Mapped[str | None] = mapped_column(String(32))
    anubhag_status: Mapped[str | None] = mapped_column(String(32))
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


class DocumentCover(Base):
    """What the cover page (page 1) of a roll says about itself.

    One row per document, independent of extraction attempts: the cover is a
    property of the file, not of any OCR run over its cards. The printed
    counts in section 4 -- male, female, third gender, total -- are the
    authority the card OCR is reconciled against on the batch page.
    """
    __tablename__ = "document_covers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    source: Mapped[str | None] = mapped_column(String(20))  # text_layer | tesseract
    error: Mapped[str | None] = mapped_column(Text)

    roll_year: Mapped[int | None] = mapped_column(Integer)
    state_code: Mapped[str | None] = mapped_column(String(10))
    state_name: Mapped[str | None] = mapped_column(Text)
    ac_code: Mapped[str | None] = mapped_column(String(10))
    ac_name: Mapped[str | None] = mapped_column(Text)
    ac_reservation: Mapped[str | None] = mapped_column(Text)
    part_number: Mapped[int | None] = mapped_column(Integer)
    pc_code: Mapped[str | None] = mapped_column(String(10))
    pc_name: Mapped[str | None] = mapped_column(Text)
    pc_reservation: Mapped[str | None] = mapped_column(Text)
    revision_year: Mapped[int | None] = mapped_column(Integer)
    qualifying_date: Mapped[str | None] = mapped_column(String(20))
    revision_type: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[str | None] = mapped_column(String(20))
    roll_identification: Mapped[str | None] = mapped_column(Text)
    sections: Mapped[list | None] = mapped_column(JSONB)
    village: Mapped[str | None] = mapped_column(Text)
    ward: Mapped[str | None] = mapped_column(Text)
    post_office: Mapped[str | None] = mapped_column(Text)
    police_station: Mapped[str | None] = mapped_column(Text)
    panchayat: Mapped[str | None] = mapped_column(Text)
    block: Mapped[str | None] = mapped_column(Text)
    tehsil: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    pin_code: Mapped[str | None] = mapped_column(String(10))
    polling_station_number: Mapped[str | None] = mapped_column(String(10))
    polling_station_name: Mapped[str | None] = mapped_column(Text)
    polling_station_address: Mapped[str | None] = mapped_column(Text)
    polling_station_type: Mapped[str | None] = mapped_column(Text)
    auxiliary_stations: Mapped[int | None] = mapped_column(Integer)
    total_pages: Mapped[int | None] = mapped_column(Integer)

    start_serial: Mapped[int | None] = mapped_column(Integer)
    end_serial: Mapped[int | None] = mapped_column(Integer)
    male: Mapped[int | None] = mapped_column(Integer)
    female: Mapped[int | None] = mapped_column(Integer)
    third_gender: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    counts_consistent: Mapped[bool | None] = mapped_column(Boolean)

    raw_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_cover_document"),
        CheckConstraint("status IN ('queued','processing','completed','failed')", name="document_covers_status"),
        Index("idx_document_covers_status", "status"),
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
