"""Safe, atomic PDF uploads into the directory watched by the pipeline."""
from __future__ import annotations

import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from fastapi import UploadFile

from app import config


CHUNK_SIZE = 1024 * 1024


class UploadValidationError(ValueError):
    pass


@dataclass(frozen=True)
class UploadSummary:
    batch_name: str
    relative_directory: str
    file_count: int
    total_bytes: int


def safe_batch_name(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", (value or "").strip())
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return value[:80] or "upload"


def safe_relative_pdf_path(filename: str | None) -> PurePosixPath:
    """Return a safe relative path while retaining browser folder uploads."""
    raw = unicodedata.normalize("NFKC", (filename or "").replace("\\", "/").strip())
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise UploadValidationError(f"Unsafe upload path: {filename or '(missing filename)'}")
    if path.suffix.lower() != ".pdf":
        raise UploadValidationError(f"Only PDF files are accepted: {path.name}")
    return path


async def save_upload_batch(files: Sequence[UploadFile], batch_label: str | None) -> UploadSummary:
    if not files:
        raise UploadValidationError("Choose at least one PDF to upload.")

    # No cap on the number of files. A 500-file limit meant a folder of 1000
    # scanned rolls was rejected outright with no way to see what it held,
    # which is the exact case the /uploads table exists to make visible. The
    # byte limits below still bound disk use; they are the guards that matter.

    root = config.DOCUMENT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory_name = f"{safe_batch_name(batch_label)}-{stamp}-{uuid.uuid4().hex[:8]}"
    batch_root = (root / directory_name).resolve()
    if batch_root.parent != root.resolve():
        raise UploadValidationError("Invalid batch directory.")
    batch_root.mkdir()

    total_bytes = 0
    saved = 0
    destinations: set[PurePosixPath] = set()
    file_limit = config.MAX_UPLOAD_FILE_MB * 1024 * 1024
    batch_limit = config.MAX_UPLOAD_BATCH_MB * 1024 * 1024

    try:
        for upload in files:
            relative = safe_relative_pdf_path(upload.filename)
            if relative in destinations:
                raise UploadValidationError(f"The upload contains the same path twice: {relative}")
            destinations.add(relative)

            destination = (batch_root / Path(*relative.parts)).resolve()
            if batch_root not in destination.parents:
                raise UploadValidationError(f"Unsafe upload path: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
            file_bytes = 0
            first_chunk = True

            try:
                with temporary.open("xb") as stream:
                    while chunk := await upload.read(CHUNK_SIZE):
                        if first_chunk:
                            first_chunk = False
                            if not chunk.startswith(b"%PDF-"):
                                raise UploadValidationError(
                                    f"{relative} is not a valid PDF file, so nothing from "
                                    f"this upload was saved."
                                )
                        file_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if file_bytes > file_limit:
                            raise UploadValidationError(
                                f"{relative} is larger than the {config.MAX_UPLOAD_FILE_MB} MB "
                                f"per-file limit, so nothing from this upload was saved."
                            )
                        if total_bytes > batch_limit:
                            raise UploadValidationError(
                                f"This upload passes the {config.MAX_UPLOAD_BATCH_MB} MB total "
                                f"limit, so nothing from it was saved. Split it into "
                                f"separate uploads."
                            )
                        stream.write(chunk)
                if file_bytes == 0:
                    raise UploadValidationError(f"{relative} is empty.")
                temporary.replace(destination)
                saved += 1
            finally:
                await upload.close()
                temporary.unlink(missing_ok=True)
    except Exception:
        shutil.rmtree(batch_root, ignore_errors=True)
        raise

    return UploadSummary(directory_name, directory_name, saved, total_bytes)
