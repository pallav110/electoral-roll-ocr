"""Two upload bugs that made the feature look broken while reporting nothing.

1. Starlette caps one multipart part at 1 MB and FastAPI never raises that
   default, so every PDF over 1 MB was rejected by the framework with a bare
   `400 {"detail":"There was an error parsing the body"}`. No file name, no
   reason, and it happens *before* the route runs, so the app's own 100 MB
   per-file limit and its all-or-nothing rollback message were unreachable.
   Scanned electoral rolls are routinely 5-20 MB, so this made the real use
   case impossible and looked like the upload box being broken.

2. `local_source_id` builds an identity from the path *relative to whichever
   directory was walked*. The uploads flow walks a fresh per-upload directory,
   where `roll_001.pdf` is relative as just `roll_001.pdf` -- exactly as it was
   for the first upload. `uq_documents_source` then rejected the second copy
   and `on_conflict_do_nothing` swallowed it, so four of six files in a fresh
   upload never got a row while `file_count` still said six.

Both fail silently. That is what these tests are for.

The multipart limit one is a pure in-process check and runs anywhere. The
discovery one needs the compose Postgres and skips without it, matching the
guard in `test_upload_batches.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

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


# ── The 1 MB framework cap ─────────────────────────────────────────────────

def test_multipart_accepts_a_pdf_larger_than_one_megabyte():
    """The default is the bug: Starlette's own 1 MB, not ours.

    Asserted against the app's advertised per-file limit rather than a
    hard-coded number, so raising MAX_UPLOAD_FILE_MB without raising this
    fails here instead of in a user's face.
    """
    from starlette.formparsers import MultiPartParser

    import app.main  # noqa: F401  -- importing is what applies the override
    from app import config

    assert MultiPartParser.max_part_size >= config.MAX_UPLOAD_FILE_MB * 1024 * 1024, (
        "Starlette's default 1 MB part cap is back in force, so every PDF "
        "over 1 MB is rejected with 'There was an error parsing the body' "
        "before the route runs and MAX_UPLOAD_FILE_MB is unreachable"
    )


def test_a_two_megabyte_part_actually_parses():
    """The number is not the whole claim -- prove a big body round-trips.

    Catches the failure mode where max_part_size looks right but the parser
    still trips on disk spooling, which is what a missing TMPDIR looks like.
    """
    import anyio
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    import app.main  # noqa: F401

    boundary = "regression-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="roll.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024) + f"\r\n--{boundary}--\r\n".encode()

    async def stream():
        # Chunked, because that is how a real request body arrives.
        for start in range(0, len(body), 64 * 1024):
            yield body[start:start + 64 * 1024]

    async def parse():
        parser = MultiPartParser(
            Headers({"content-type": f"multipart/form-data; boundary={boundary}"}),
            stream(),
            max_part_size=MultiPartParser.max_part_size,
        )
        return await parser.parse()

    form = anyio.run(parse)
    files = form.getlist("files")
    assert [f.filename for f in files] == ["roll.pdf"]
    assert files[0].size == 2 * 1024 * 1024 + 9  # payload + "%PDF-1.4\n"


# ── Same filename, two uploads ─────────────────────────────────────────────


def test_the_same_filename_can_be_uploaded_twice(tmp_path, monkeypatch):
    """An identical second upload is skipped instead of starting OCR twice.

    Re-uploading the same bytes under the same filename or a Windows-generated
    `(1)` filename must not create a second processing document. The canonical
    first document remains the only OCR job; the second batch reports zero new
    documents for that duplicate content.
    """
    import pytest

    if not _db_available():
        pytest.skip("needs the compose Postgres; run the stack first")

    from app import config
    from app.db import SessionLocal
    from app.models import Document, UploadBatch
    from app.workflow import discover_upload_batch

    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    import uuid as _uuid

    counts = []
    for attempt in ("first", "second"):
        batch_id = _uuid.uuid4()
        directory = tmp_path / f"{attempt}-{batch_id.hex[:8]}"
        directory.mkdir(parents=True)
        # Only one file: same name, byte-identical, different upload. The
        # collision is on identity, so a second file would not help.
        (directory / "roll_001.pdf").write_bytes(b"%PDF-1.4\n" + b"a" * 64)

        with SessionLocal.begin() as db:
            db.add(UploadBatch(id=batch_id, label=f"regression {attempt}", source="files",
                               relative_directory=directory.name, file_count=1))

        expected = 1 if attempt == "first" else 0
        assert discover_upload_batch(batch_id) == expected, (
            f"the {attempt} upload registered an unexpected number of new "
            "documents for identical PDF content"
        )
        with SessionLocal() as db:
            counts.append(db.query(Document).filter(
                Document.upload_batch_id == batch_id).count())

    assert counts == [1, 0], (
        "identical PDF bytes must have one canonical document, not two OCR jobs"
    )


def test_source_ids_differ_across_upload_directories(tmp_path):
    """The identity is the fix, stated directly rather than through the DB."""
    from app.workflow import local_source_id

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "roll_001.pdf").write_bytes(b"%PDF-1.4\n")
    (second / "roll_001.pdf").write_bytes(b"%PDF-1.4\n")

    digest = "same-digest"
    a = local_source_id(first, first / "roll_001.pdf", digest, "first-20260101")
    b = local_source_id(second, second / "roll_001.pdf", digest, "second-20260101")
    assert a != b, "two uploads of one filename still share a source identity"

    # The scope must not break the property it was added for: the same file
    # under the same scope still deduplicates across re-scans.
    assert a == local_source_id(first, first / "roll_001.pdf", digest, "first-20260101")