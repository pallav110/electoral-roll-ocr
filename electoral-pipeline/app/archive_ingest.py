"""Poll an S3-compatible bucket and safely materialize ZIP-contained PDFs."""
from __future__ import annotations

import hashlib
import logging
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app import config
from app.db import SessionLocal
from app.models import ArchiveIngestion, Document, UploadBatch, now
from app.uploads import UploadValidationError, safe_batch_name, safe_relative_pdf_path


log = logging.getLogger(__name__)
COPY_CHUNK_SIZE = 1024 * 1024


def s3_client():
    if not config.MINIO_ACCESS_KEY or not config.MINIO_SECRET_KEY:
        raise RuntimeError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required")
    return boto3.client(
        "s3",
        endpoint_url=config.MINIO_ENDPOINT,
        aws_access_key_id=config.MINIO_ACCESS_KEY,
        aws_secret_access_key=config.MINIO_SECRET_KEY,
        region_name=config.MINIO_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_bucket(client) -> None:
    try:
        client.head_bucket(Bucket=config.MINIO_BUCKET)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=config.MINIO_BUCKET)


def extract_pdf_archive(archive_path: Path, staging_root: Path) -> tuple[int, int]:
    """Extract only safe PDFs, preserving the ZIP's directory hierarchy.

    No size or count ceiling of any kind. A full booth archive is what arrives
    here -- several GB is ordinary -- and a cap produced exactly one outcome:
    a Failed row whose error message was the limit, with the extracted PDFs
    never reaching /uploads. The only real bound is free disk, and running out
    of it fails loudly on write, which is a more honest error than refusing in
    advance.
    """
    pdf_count = 0
    extracted_bytes = 0
    staging_root.mkdir(parents=True)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = [info for info in archive.infolist() if not info.is_dir() and Path(info.filename).suffix.lower() == ".pdf"]
            if not members:
                raise UploadValidationError("The ZIP does not contain any PDF files.")

            seen = set()
            for info in members:
                relative = safe_relative_pdf_path(info.filename)
                if relative in seen:
                    raise UploadValidationError(f"The ZIP contains the same PDF path twice: {relative}")
                seen.add(relative)
                file_mode = info.external_attr >> 16
                if stat.S_ISLNK(file_mode):
                    raise UploadValidationError(f"Symbolic links are not allowed in ZIP files: {relative}")
                if info.flag_bits & 0x1:
                    raise UploadValidationError(f"Encrypted ZIP entries are not supported: {relative}")

                destination = (staging_root / Path(*relative.parts)).resolve()
                if staging_root.resolve() not in destination.parents:
                    raise UploadValidationError(f"Unsafe ZIP path: {relative}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as source, destination.open("xb") as target:
                    while chunk := source.read(COPY_CHUNK_SIZE):
                        written += len(chunk)
                        extracted_bytes += len(chunk)
                        target.write(chunk)
                with destination.open("rb") as stream:
                    if stream.read(5) != b"%PDF-":
                        raise UploadValidationError(f"{relative} is not a valid PDF file.")
                if written != info.file_size:
                    raise UploadValidationError(f"ZIP entry size changed while reading: {relative}")
                pdf_count += 1
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise

    return pdf_count, extracted_bytes


def _pdfs_in(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"]


def _register_batch(object_key: str, directory_name: str, pdf_count: int):
    """The UploadBatch for one extracted archive, created once.

    A second pass — the folder was already on disk from a run whose database
    rows were wiped — must reuse the batch rather than insert another, or the
    upload page would list the same ZIP twice.
    """
    with SessionLocal.begin() as db:
        existing = db.scalar(
            select(UploadBatch.id).where(UploadBatch.relative_directory == directory_name)
        )
        if existing:
            return existing
        batch = UploadBatch(
            label=Path(object_key).stem or directory_name,
            source="minio",
            relative_directory=directory_name,
            file_count=pdf_count,
        )
        db.add(batch)
        db.flush()
        return batch.id


def _attach_existing_documents(batch_id, final_root: Path) -> None:
    """Point PDFs already registered under this folder at the new batch.

    discover_upload_batch skips a file it has seen before and leaves its
    upload_batch_id alone. After a database wipe the hourly scan re-registers
    those PDFs with no batch, and the ZIP then has nothing for "Open" to
    link to. This attaches only the unattached rows, then numbers them the
    same way a fresh extract does.
    """
    prefix = str(final_root.resolve()) + "/"
    with SessionLocal.begin() as db:
        db.execute(
            update(Document)
            .where(Document.source_path.startswith(prefix))
            .where(Document.upload_batch_id.is_(None))
            .values(upload_batch_id=batch_id)
        )
        db.execute(text(
            "UPDATE documents SET batch_index = s.rn FROM ("
            "  SELECT id, row_number() OVER (ORDER BY source_path) AS rn"
            "  FROM documents WHERE upload_batch_id = :bid"
            ") s WHERE documents.id = s.id"
        ), {"bid": batch_id})


def _finish_archive(ingestion_id, object_key: str, directory_name: str,
                    final_root: Path, pdf_count: int, extracted_bytes: int, *,
                    rescan: bool = True) -> None:
    batch_id = _register_batch(object_key, directory_name, pdf_count)
    if rescan:
        from app.workflow import discover_upload_batch
        discover_upload_batch(batch_id)
    _attach_existing_documents(batch_id, final_root)
    with SessionLocal.begin() as db:
        row = db.get(ArchiveIngestion, ingestion_id)
        row.status = "completed"
        row.pdf_count = pdf_count
        row.extracted_bytes = extracted_bytes
        row.output_directory = directory_name
        row.completed_at = now()
        row.error_message = None


def _process_ingestion(client, ingestion_id) -> bool:
    with SessionLocal.begin() as db:
        row = db.get(ArchiveIngestion, ingestion_id)
        if not row or row.status != "pending":
            return False
        row.status = "processing"
        row.started_at = now()
        bucket, object_key, etag = row.bucket, row.object_key, row.etag

    identity = hashlib.sha256(f"{bucket}\0{object_key}\0{etag}".encode()).hexdigest()[:12]
    directory_name = f"minio-{safe_batch_name(Path(object_key).stem)}-{identity}"
    final_root = config.DOCUMENT_ROOT / directory_name
    staging_root = config.DOCUMENT_ROOT / f".{directory_name}.staging"
    archive_path = None

    try:
        config.DOCUMENT_ROOT.mkdir(parents=True, exist_ok=True)
        # The folder surviving a database wipe is the normal case, not an
        # error: the PDFs are already extracted. Register them and finish,
        # instead of failing forever and hiding the ZIP from the upload page.
        if final_root.is_dir():
            pdfs = _pdfs_in(final_root)
            if not pdfs:
                raise RuntimeError(
                    f"Archive output directory already exists and contains no PDFs: {directory_name}"
                )
            prefix = str(final_root.resolve()) + "/"
            with SessionLocal() as db:
                already = db.scalar(
                    select(func.count()).select_from(Document).where(Document.source_path.startswith(prefix))
                ) or 0
            _finish_archive(
                ingestion_id, object_key, directory_name, final_root,
                len(pdfs), sum(p.stat().st_size for p in pdfs),
                rescan=already < len(pdfs),
            )
            if config.MINIO_DELETE_AFTER_INGEST:
                client.delete_object(Bucket=bucket, Key=object_key)
            return True
        if staging_root.exists():
            shutil.rmtree(staging_root)
        with tempfile.NamedTemporaryFile(prefix="electoral-archive-", suffix=".zip", delete=False) as temporary:
            archive_path = Path(temporary.name)
            client.download_fileobj(bucket, object_key, temporary)

        pdf_count, extracted_bytes = extract_pdf_archive(archive_path, staging_root)
        staging_root.replace(final_root)
        # One UploadBatch per archive, so ZIP-sourced PDFs show up under their
        # own heading instead of as unattributed strays that only the hourly
        # discover beat would eventually pick up.
        _finish_archive(
            ingestion_id, object_key, directory_name, final_root, pdf_count, extracted_bytes,
        )

        if config.MINIO_DELETE_AFTER_INGEST:
            client.delete_object(Bucket=bucket, Key=object_key)
        return True
    except Exception as exc:
        log.exception("Failed to ingest s3://%s/%s", bucket, object_key)
        shutil.rmtree(staging_root, ignore_errors=True)
        with SessionLocal.begin() as db:
            row = db.get(ArchiveIngestion, ingestion_id)
            if row:
                row.status = "failed"
                row.error_message = f"{type(exc).__name__}: {exc}"[:4000]
                row.completed_at = now()
        return False
    finally:
        if archive_path:
            archive_path.unlink(missing_ok=True)


def ingest_minio_archives() -> int:
    if not config.MINIO_ENABLED:
        return 0

    client = s3_client()
    ensure_bucket(client)
    prefix = f"{config.MINIO_PREFIX}/" if config.MINIO_PREFIX else ""
    pending_ids = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.MINIO_BUCKET, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.lower().endswith(".zip"):
                continue
            etag = str(item.get("ETag", "")).strip('"')
            with SessionLocal.begin() as db:
                ingestion_id = db.execute(
                    insert(ArchiveIngestion)
                    .values(
                        bucket=config.MINIO_BUCKET,
                        object_key=key,
                        etag=etag,
                        object_size=item.get("Size"),
                        status="pending",
                    )
                    .on_conflict_do_nothing(constraint="uq_archive_ingestion_object_version")
                    .returning(ArchiveIngestion.id)
                ).scalar_one_or_none()
                if ingestion_id is None:
                    # The unique key is (bucket, object_key, etag), so an object
                    # that failed once occupies its own row forever and the
                    # on_conflict_do_nothing above skips it on every subsequent
                    # poll -- the beat returns 0 and the archive never gets a
                    # second attempt, no matter what the limits are now set to.
                    # Raising a limit and re-uploading the same bytes therefore
                    # looks like it did nothing. Re-arm a failed row so the next
                    # poll retries it; completed and processing rows are left
                    # alone so a healthy archive is not re-extracted.
                    ingestion_id = db.execute(
                        update(ArchiveIngestion)
                        .where(
                            ArchiveIngestion.bucket == config.MINIO_BUCKET,
                            ArchiveIngestion.object_key == key,
                            ArchiveIngestion.etag == etag,
                            ArchiveIngestion.status == "failed",
                        )
                        .values(status="pending", error_message=None, completed_at=None)
                        .returning(ArchiveIngestion.id)
                    ).scalar_one_or_none()
                if ingestion_id:
                    pending_ids.append(ingestion_id)

    completed = sum(1 for ingestion_id in pending_ids if _process_ingestion(client, ingestion_id))
    if completed:
        from app.workflow import discover
        discover()
    return completed
