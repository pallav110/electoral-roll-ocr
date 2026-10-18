"""Exact duplicate PDFs must not become separate processing documents."""
from pathlib import Path
import sys
import uuid

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _db_available() -> bool:
    try:
        from sqlalchemy import text
        from app.db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def test_discovery_skips_same_bytes_under_different_filenames(tmp_path):
    """Different names do not make identical PDF content a second OCR job.

    Windows preserves repeated downloads as `HIN-107-WI.pdf` and
    `HIN-107-WI (1).pdf`. Discovery assigns different source_document_id values
    for those paths, so the conditional database hash constraint does not stop
    them. The discovery layer must reject the second content hash before it can
    create a document/session/unit.
    """
    if not _db_available():
        pytest.skip("needs the compose Postgres; run the stack first")

    from app.db import SessionLocal
    from app.models import Document
    from app.workflow import _discover_under, delete_documents

    root = tmp_path / "duplicates"
    root.mkdir()
    payload = b"%PDF-1.4\nidentical-roll-bytes-" + uuid.uuid4().hex.encode()
    (root / "HIN-107-WI.pdf").write_bytes(payload)
    (root / "HIN-107-WI (1).pdf").write_bytes(payload)

    try:
        assert _discover_under(root, "duplicate regression", scan_root_id=None) == 1
        with SessionLocal() as db:
            docs = db.query(Document).filter(
                Document.file_name.in_(
                    ["HIN-107-WI.pdf", "HIN-107-WI (1).pdf"]
                )
            ).all()
            assert len(docs) == 1
            assert docs[0].file_name in {"HIN-107-WI.pdf", "HIN-107-WI (1).pdf"}
    finally:
        with SessionLocal() as db:
            ids = [d.id for d in db.query(Document).filter(
                Document.file_name.in_(
                    ["HIN-107-WI.pdf", "HIN-107-WI (1).pdf"]
                )
            ).all()]
        if ids:
            delete_documents(ids)
