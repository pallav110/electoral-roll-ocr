"""Local PDF discovery and read access. Replace adapters here for S3, Drive, etc."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from app import config


def resolve_document_path(stored_path: str) -> Path:
    """Map a stored path to a path readable in this process (e.g. host path → mount)."""
    root = config.DOCUMENT_ROOT.resolve()
    normalized = stored_path.replace("/pdfs/pdfs/", "/pdfs/")
    raw = Path(normalized)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(root / raw)
    candidates.append(root / raw.name)
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    name = raw.name
    if name:
        for hit in root.rglob(name):
            if hit.is_file():
                return hit.resolve()
    return raw


@contextmanager
def open_pdf(stored_path: str) -> Iterator[BinaryIO]:
    path = resolve_document_path(stored_path)
    with path.open("rb") as stream:
        yield stream


def file_fingerprint(path: Path) -> tuple[str, int, datetime]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    return digest.hexdigest(), stat.st_size, modified


def iter_discoverable_pdfs(root: Path | None = None) -> Iterator[Path]:
    root = (root or config.DOCUMENT_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".pdf":
            yield path
