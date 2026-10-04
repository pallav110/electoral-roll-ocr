"""Database-owned state transitions. Redis carries IDs only."""
import hashlib
import logging
import os
import re
import shutil
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from pypdf import PdfReader
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app import config
from app.db import SessionLocal
from app.extractor import ExtractionError, extract, validate_response
from app.models import Document, ElectoralDocumentMetadata, ElectoralRecord, ExtractionSession, ExtractionUnit, ProcessingEvent, RawResponse, ScanRoot, now
from app.normalize import map_metadata, map_record


log = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"

log.info(f"[WORKFLOW] Worker ID initialized: {WORKER_ID}")
print(f"[WORKFLOW] Worker ID initialized: {WORKER_ID}")


def event(db, kind, document_id=None, session_id=None, unit_id=None, message=None, details=None):
    log.info(f"[EVENT] {kind} - document_id={document_id}, session_id={session_id}, unit_id={unit_id}, message={message}")
    print(f"[EVENT] {kind} - document_id={document_id}, session_id={session_id}, unit_id={unit_id}, message={message}")
    db.add(ProcessingEvent(document_id=document_id, session_id=session_id, extraction_unit_id=unit_id, service="pipeline", event_type=kind, message=message, details=details))


def _resolve_scan_path(raw: str) -> PurePosixPath:
    """Turn a folder path typed in the UI into one the container can open.

    Returns a PurePosixPath on purpose, and never probes the filesystem.
    Two reasons, both learned the hard way:

    1. The process that walks this tree runs in a Linux container. Building
       the result with pathlib.Path("/host/c") on a Windows host yields a
       *WindowsPath*, which stringifies to backslashes -- so the path stored
       in the database would be "\\host\\c\\..." and the Linux worker could
       not open it. PurePosixPath is the same on both, so the value written
       here is the value the worker will read, wherever the tests ran.

    2. The Windows branch is pure string work. Path.is_absolute() on Linux
       cannot tell "C:\\..." from a relative name, and on Windows the answer
       depends on which drive is current. Rewriting first and asking about
       the result is the only order that is correct in both places.

    Accepted shapes on a Windows host (HOST_MOUNT_STYLE=drive):

      "C:\\Users\\me\\Rolls"     -> /host/c/Users/me/Rolls
      "c:/Users/me/Rolls"        -> /host/c/Users/me/Rolls
      "/data/rolls"              -> /data/rolls   (already container-native)
      "\\\\server\\share\\rolls"  -> left alone; a UNC path is not under a
                                    drive mount and cannot be rewritten here

    Accepted shapes on a Linux host (HOST_MOUNT_STYLE=posix):

      "/srv/rolls"               -> /host/srv/rolls
      "/home/me/Desktop/xyz"     -> /host/home/me/Desktop/xyz

    A path that is ALREADY under the mount root is returned untouched, which is
    what keeps this idempotent in both styles: saving a root and then typing the
    resolved value back must not nest the prefix a second time.

    The Windows drive letter becomes a *path segment* under the mount root, so
    compose mounts C: at /host/c and a D: drive at /host/d. That mapping is why
    the prefix is a directory rather than the mount itself: a fixed mount point
    cannot represent more than one drive. Linux has no drive letters, so the
    rule there is the uniform one -- append the absolute path to the mount root.
    """
    text = (raw or "").strip().strip('"')
    if not text:
        raise ValueError("empty path")

    text = text.replace("\\", "/")

    # A UNC path points at a share, not at a drive letter. There is no
    # correct rewrite for it, so it is passed through and will fail with a
    # clear "does not exist" rather than being silently turned into garbage.
    if text.startswith("//"):
        return PurePosixPath(text)

    mount_root = PurePosixPath(config.HOST_MOUNT_ROOT)

    # Already resolved -- the value the UI would show back after a save. Checked
    # before the rewrite below in both styles, or a resolved root would gain a
    # second copy of the prefix every time it was saved.
    if text == mount_root.as_posix() or text.startswith(mount_root.as_posix() + "/"):
        return PurePosixPath(text)

    if config.HOST_MOUNT_STYLE == "posix":
        # Linux. A path that is already container-internal must be left alone.
        # Without this, "/data/pdfs" would be read as the HOST's /data/pdfs and
        # the sample mount would silently become an empty directory -- the scan
        # would report zero PDFs with no error anywhere.
        if text.startswith("/"):
            # Container-owned prefixes are the ones compose binds a named
            # volume to. They are not host paths, so they are exempt.
            container_roots = ("/data/pdfs", "/data/ingest", "/results")
            if not any(text == c or text.startswith(c + "/") for c in container_roots):
                return mount_root / text.lstrip("/")
            return PurePosixPath(text)

        # A drive letter on a Linux host. There is no drive to resolve, but the
        # text is still a well-formed relative name, so it falls through to the
        # fallback root below and reports a plain miss rather than producing a
        # "/host/C:" segment that cannot exist.
        return PurePosixPath(config.DOCUMENT_ROOT.as_posix()) / text

    if text.startswith("/"):
        return PurePosixPath(text)

    if re.match(r"^[A-Za-z]:", text):
        drive = text[0].lower()
        rest = text[2:].lstrip("/")
        return mount_root / drive / rest

    # No drive and no leading slash: relative to the fallback root, which is
    # what a bare "rolls" most likely means.
    return PurePosixPath(config.DOCUMENT_ROOT.as_posix()) / text


def discover(roots: list[str] | None = None) -> int:
    """Find PDFs by content hash across every configured scan root.

    Same filename with changed bytes is a new document, unchanged.

    `roots` overrides the configured set for a one-off scan; the normal path
    passes None and reads the scan_roots table, falling back to
    config.DOCUMENT_ROOT only when no root has ever been configured. That
    fallback is what keeps a fresh deployment working with no setup at all.

    The walk is rglob, which is fully recursive to any depth -- a root with a
    thousand folders under it is walked the same as a flat one. What is NOT
    unbounded is the set of roots, and that is what this function now takes.

    If a path is not already visible inside the container (not under any
    bind mount), the PDFs are copied into the ingest folder so they become
    discoverable without needing a new mount. This is what makes "any path"
    work.
    """
    # (path -> scan_root_id). The id rides along because a document must
    # record WHICH folder produced it, and re-querying per file inside the
    # walk would mean one round trip per PDF -- thousands for a large roll.
    # A one-off scan (roots passed in explicitly) has no ScanRoot row to
    # point at, so its id stays absent and the documents it finds land in
    # "no folder", which is the honest answer rather than a guess.
    root_ids: dict[str, int | None] = {}

    if roots is None:
        with SessionLocal() as db:
            rows = db.scalars(select(ScanRoot).where(ScanRoot.enabled.is_(True)).order_by(ScanRoot.id)).all()
            roots = [r.path for r in rows]
            root_ids = {r.path: r.id for r in rows}

        if not roots:
            log.info("[DISCOVER] No scan roots configured, falling back to %s", config.DOCUMENT_ROOT)
            print(f"[DISCOVER] No scan roots configured, falling back to {config.DOCUMENT_ROOT}")
            # Bootstrapping only. A configured root is never created for the
            # user: if they typed a path that does not exist, that is a typo
            # and silently making the directory would hide it behind a scan
            # that finds nothing and reports success.
            try:
                config.DOCUMENT_ROOT.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.exception("[DISCOVER] Could not create fallback root %s", config.DOCUMENT_ROOT)
            roots = [str(config.DOCUMENT_ROOT)]
            root_ids = {str(config.DOCUMENT_ROOT): None}
        else:
            log.info("[DISCOVER] Scanning %d configured folder(s)", len(roots))
            print(f"[DISCOVER] Scanning {len(roots)} configured folder(s)")

    found = 0
    for raw in roots:
        try:
            root = _resolve_scan_path(raw)
        except ValueError:
            log.warning("[DISCOVER] Ignoring blank scan root")
            continue

        scan_root_id = root_ids.get(raw)

        # PurePosixPath carries no filesystem methods; Path is what actually
        # walks. The conversion happens here, once, at the boundary between
        # "a path string that is always POSIX" and "a path we touch the disk
        # with" -- never inside the rewrite itself, which must stay testable
        # on a host with no such mount.
        root = Path(str(root))

        if not root.exists() or not root.is_dir():
            # The path the user typed isn't visible inside the container.
            # Copy the PDFs into the ingest folder so they become discoverable.
            # This is the "any path" feature: no mount required, just a copy.
            message = f"folder not visible inside container: {root}"
            log.info("[DISCOVER] %s — copying PDFs into ingest", message)
            print(f"[DISCOVER] {raw!r}: not mounted; copying PDFs into ingest")
            copied = _copy_pdfs_into_ingest(raw, root)
            if copied:
                # The copied files are now under INGEST_ROOT; scan that instead.
                ingest_root = config.INGEST_ROOT / _safe_name(raw)
                found += _discover_under(ingest_root, raw, scan_root_id=scan_root_id)
                _record_scan_result(raw, copied, None)
            else:
                _record_scan_result(raw, 0, "no PDFs found to copy")
            continue

        found += _discover_under(root, raw, scan_root_id=scan_root_id)
        # _discover_under already recorded this root's outcome, including any
        # unreadable files. Recording it AGAIN here with no error message
        # overwrote that warning with a clean result, which is why a folder of
        # OneDrive placeholders still reported "found=0" with nothing wrong
        # shown. Only the document count is folded into the running total.

    log.info(f"[DISCOVER] Discovery complete, found {found} new documents")
    print(f"[DISCOVER] Discovery complete, found {found} new documents")
    return found


def _safe_name(raw: str) -> str:
    """Sanitize a raw path into a safe directory name for the ingest folder."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip().strip('"')).strip("_")


def _copy_pdfs_into_ingest(raw: str, resolved: Path) -> int:
    """Copy every PDF under a host path into the container's ingest folder.

    Returns the number of files copied. If the host path isn't readable at
    all (e.g. a path that isn't mounted), returns 0 and logs why.
    """
    # The worker's view of the host filesystem is what we have via bind mounts.
    # If the resolved path exists, use it directly. If not, we can't read it --
    # the user typed a path that this deployment has no mount for.
    host_path = resolved if resolved.exists() and resolved.is_dir() else None

    if host_path is None:
        # A container cannot copy a file it cannot see, so there is nothing
        # to fall back to here: this is a genuine "no access", not a lookup we
        # failed to do. The message names the path we actually looked for,
        # because "drive not mounted" would be actively wrong on Linux, where
        # the usual cause is a path that simply does not exist.
        log.warning("[DISCOVER] Cannot reach host path %r (looked for %s)", raw, resolved)
        print(
            f"[DISCOVER] Cannot reach {raw!r} — looked for {resolved} inside the container "
            f"and it is not there. Check the path exists and that it is covered by a mount "
            f"in docker-compose.yml."
        )
        return 0

    ingest_dir = config.INGEST_ROOT / _safe_name(raw)
    ingest_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for pdf in host_path.rglob("*.pdf"):
        try:
            dest = ingest_dir / pdf.name
            # If a file with the same name already exists, skip it — the
            # content-hash check in _discover_under will deduplicate anyway.
            if not dest.exists():
                shutil.copy2(pdf, dest)
                copied += 1
        except OSError:
            log.exception("[DISCOVER] Failed to copy %s", pdf)

    log.info("[DISCOVER] Copied %d PDF(s) from %s into %s", copied, host_path, ingest_dir)
    print(f"[DISCOVER] Copied {copied} PDF(s) from {host_path} into {ingest_dir}")
    return copied


def _record_scan_result(raw: str, count: int, error: str | None = None) -> None:
    """Write back what a scan of this root actually found, so the UI shows
    reality rather than what the user assumed. Best effort: a failure to
    record must never abort the scan itself."""
    try:
        with SessionLocal.begin() as db:
            root = db.scalar(select(ScanRoot).where(ScanRoot.path == raw))
            if root is None:
                return
            root.last_found = count
            root.last_scanned_at = now()
            root.last_error = error
    except Exception:
        log.exception("[DISCOVER] Could not record scan result for %s", raw)


def _discover_under(root: Path, label: str, scan_root_id: int | None = None) -> int:
    """Walk one resolved root and register every PDF beneath it.

    Unreadable files are reported, not silently dropped. A file that cannot be
    opened and a folder containing no PDFs both used to surface as "found=0",
    which the UI renders identically to success.

    `scan_root_id` is the folder this walk belongs to, recorded on every
    document so a folder view can group by it exactly rather than by guessing
    from source_path. It is None for a one-off scan of an unregistered path,
    which is why the column is nullable.
    """
    log.info("[DISCOVER] Walking %s (from %r)", root, label)
    print(f"[DISCOVER] Walking {root}")
    found = 0
    unreadable: list[str] = []

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue

        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            stat = path.stat()
            values = dict(source_type="local", source_document_id=None, source_path=str(path.resolve()), file_name=path.name,
                          file_hash=digest.hexdigest(), file_size=stat.st_size, source_modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc), status="pending", scan_root_id=scan_root_id)

            log.debug(f"[DISCOVER] File {path.name}: size={stat.st_size} bytes, hash={digest.hexdigest()[:16]}...")
            print(f"[DISCOVER] File {path.name}: size={stat.st_size} bytes, hash={digest.hexdigest()[:16]}...")

            with SessionLocal.begin() as db:
                result = db.execute(insert(Document).values(**values).on_conflict_do_nothing(index_elements=[Document.source_type, Document.file_hash], index_where=(Document.source_document_id.is_(None) & Document.file_hash.is_not(None))).returning(Document.id))
                document_id = result.scalar_one_or_none()
                if document_id:
                    event(db, "DOCUMENT_DISCOVERED", document_id, message=path.name)
                    found += 1
                    log.info(f"[DISCOVER] New document registered: {document_id} for file {path.name}")
                else:
                    log.debug(f"[DISCOVER] Already registered: {path.name}")
        except OSError as exc:
            # Almost always a cloud placeholder -- OneDrive/Dropbox Files
            # On-Demand reports Errno 5 on read through a bind mount, because
            # the containerised filesystem driver cannot trigger the recall.
            # The file is intact on the host; it is only unreachable from here.
            unreadable.append(path.name)
            log.warning("[DISCOVER] Could not read %s (%s)", path, exc.strerror or exc)
            print(f"[DISCOVER] SKIP unreadable: {path}")

    if unreadable:
        _record_scan_result(
            label,
            found,
            f"{len(unreadable)} PDF(s) could not be read (cloud placeholders?): "
            + ", ".join(unreadable[:3])
            + ("..." if len(unreadable) > 3 else ""),
        )
    else:
        _record_scan_result(label, found)
    return found


def dispatch_documents(limit=100):
    log.info(f"[DISPATCH_DOCS] Starting document dispatch, limit={limit}")
    print(f"[DISPATCH_DOCS] Starting document dispatch, limit={limit}")
    
    ids = []
    with SessionLocal.begin() as db:
        docs = db.scalars(select(Document).where(or_(Document.status == "pending", (Document.status == "retry") & (Document.next_retry_at <= now()))).order_by(Document.priority, Document.discovered_at).with_for_update(skip_locked=True).limit(limit)).all()
        
        log.info(f"[DISPATCH_DOCS] Found {len(docs)} documents to dispatch")
        print(f"[DISPATCH_DOCS] Found {len(docs)} documents to dispatch")
        
        for doc in docs:
            log.info(f"[DISPATCH_DOCS] Processing document {doc.id}: status={doc.status}, attempts={doc.attempt_count}/{doc.max_attempts}")
            print(f"[DISPATCH_DOCS] Processing document {doc.id}: status={doc.status}, attempts={doc.attempt_count}/{doc.max_attempts}")
            
            if doc.attempt_count >= doc.max_attempts:
                doc.status = "failed"
                event(db, "DOCUMENT_FAILED", doc.id, message=f"Max attempts ({doc.max_attempts}) reached")
                log.warning(f"[DISPATCH_DOCS] Document {doc.id} marked as failed (max attempts reached)")
                print(f"[DISPATCH_DOCS] Document {doc.id} marked as failed (max attempts reached)")
                continue
            
            doc.status = "queued"
            doc.attempt_count += 1
            doc.next_retry_at = None
            doc.current_session_id = None
            # Stamped here, in the same transaction as the "queued" transition,
            # rather than only inside recover()'s republish branch.
            #
            # recover() compares this against a 60-second cutoff to tell a publish
            # the broker dropped from one still in flight. Nothing used to write
            # it, so the comparison was against NULL and never true: a document
            # whose message was lost stayed "queued" forever, with no worker and
            # no timer, waiting on a future dispatch that selects only "pending"
            # and "retry".
            #
            # The worker does not stamp it on pickup. locked_at means "last handed
            # to the broker", and that is what recover()'s cutoff is comparing --
            # a document whose worker is alive shows status "processing" and
            # leaves this branch entirely.
            doc.locked_at = now()
            doc.locked_by = WORKER_ID
            session = ExtractionSession(document_id=doc.id, attempt_number=doc.attempt_count, status="created")
            db.add(session)
            db.flush()
            doc.current_session_id = session.id
            event(db, "SESSION_CREATED", doc.id, session.id, message=f"Attempt {doc.attempt_count}")
            ids.append((str(doc.id), str(session.id)))
            
            log.info(f"[DISPATCH_DOCS] Document {doc.id} dispatched with session {session.id}")
            print(f"[DISPATCH_DOCS] Document {doc.id} dispatched with session {session.id}")
    
    log.info(f"[DISPATCH_DOCS] Dispatch complete, {len(ids)} documents dispatched")
    print(f"[DISPATCH_DOCS] Dispatch complete, {len(ids)} documents dispatched")
    
    from app.tasks import process_document
    for doc_id, sess_id in ids:
        try:
            process_document.delay(doc_id, sess_id)
            log.debug(f"[DISPATCH_DOCS] Document {doc_id} task queued with session {sess_id}")
            print(f"[DISPATCH_DOCS] Document {doc_id} task queued with session {sess_id}")
        except Exception:
            log.exception("Queue publish failed; queued-job recovery will republish %s", doc_id)
            print(f"[DISPATCH_DOCS] ERROR: Queue publish failed for document {doc_id}")
    
    return len(ids)


def process_document_job(document_id, session_id):
    log.info(f"[PROCESS_DOC] Starting document job for document_id={document_id}, session_id={session_id}")
    print(f"📄 Starting to process PDF document...")
    
    # Convert to UUID if string
    if isinstance(document_id, str):
        document_id = uuid.UUID(document_id)
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    
    with SessionLocal.begin() as db:
        doc = db.get(Document, document_id, with_for_update=True)
        session = db.get(ExtractionSession, session_id, with_for_update=True)
        
        if not doc or not session or doc.current_session_id != session.id or doc.status != "queued" or session.status != "created":
            log.warning(f"[PROCESS_DOC] Document {document_id} or session {session_id} not in expected state, skipping")
            print(f"[PROCESS_DOC] Document {document_id} or session {session_id} not in expected state, skipping")
            return
        
        log.info(f"[PROCESS_DOC] Document {document_id} and session {session_id} validated, starting processing")
        print(f"   Document validated, beginning extraction...")
        
        doc.status = "processing"
        doc.processing_started_at = now()
        doc.heartbeat_at = now()
        session.status = "processing"
        session.started_at = now()
        session.heartbeat_at = now()
        session.worker_id = WORKER_ID
        event(db, "SESSION_STARTED", doc.id, session.id)
        path = doc.source_path
        
        log.info(f"[PROCESS_DOC] Document status set to processing, path={doc.source_path}")
        print(f"   Processing file: {doc.file_name}")
    
    try:
        # TODO(document-store): Replace this local path read with a fetch/stream adapter
        # when using S3, Drive or another configured document store.
        log.info(f"[PROCESS_DOC] Reading PDF to count pages from: {doc.source_path}")
        print(f"   Counting pages in PDF...")
        
        with open(path, "rb") as stream:
            pages = len(PdfReader(stream).pages)
        
        log.info(f"[PROCESS_DOC] PDF has {pages} pages")
        print(f"   PDF has {pages} pages - splitting into work units...")
        
        if pages < 1:
            log.error(f"[PROCESS_DOC] PDF has no pages")
            print(f"[PROCESS_DOC] PDF has no pages")
            raise ExtractionError("INVALID_PDF", "PDF has no pages", False)
    except Exception as exc:
        code = exc.code if isinstance(exc, ExtractionError) else "INVALID_PDF"
        log.error(f"[PROCESS_DOC] Error reading PDF: {str(exc)}, code={code}")
        print(f"[PROCESS_DOC] Error reading PDF: {str(exc)}, code={code}")
        fail_document_setup(document_id, session_id, code, str(exc))
        return
    
    with SessionLocal.begin() as db:
        doc = db.get(Document, document_id, with_for_update=True)
        session = db.get(ExtractionSession, session_id, with_for_update=True)
        
        if doc.current_session_id != session.id or session.status != "processing":
            log.warning(f"[PROCESS_DOC] Session state changed before unit creation, aborting")
            print(f"[PROCESS_DOC] Session state changed before unit creation, aborting")
            return
        
        # Progress is measured against pages that actually carry voter
        # cards. Counting the 2 cover pages makes the bar top out at
        # 20/22 = 91% and never reach 100%, which reads as a stuck job.
        # Voter cards start from page 3, skip pages 1-2
        start_page = 3 if pages >= 3 else 1
        session.pages_total = pages - start_page + 1
        units = []
        for index, start in enumerate(range(start_page, pages + 1, config.PAGES_PER_UNIT), 1):
            # Created already-"queued" so the unit is accepted by process_unit
            # on first delivery. process_unit rejects any unit that is not
            # "queued", and the model default is "pending", so the first
            # delivery of a freshly-created unit is thrown away. The beat
            # sweep (dispatch_pending_units, every 10s) then sets pending ->
            # queued and republishes, so the unit is DELAYED by up to one
            # sweep, not lost. Setting the status here removes that delay and
            # the wasted first publish. Measured on a 7-unit roll: created ->
            # queued was a uniform ~4.9s without this, ~0s with it.
            unit = ExtractionUnit(session_id=session.id, unit_number=index, page_from=start, page_to=min(start + config.PAGES_PER_UNIT - 1, pages),
                status="queued",
                queued_at=now(),
            )
            db.add(unit)
            units.append(unit)
        
        event(db, "UNITS_CREATED", doc.id, session.id, message=f"{pages} pages split into {index} units")
        log.info(f"[PROCESS_DOC] Created {len(units)} extraction units for {pages} pages")
        print(f"   Split into {len(units)} work units for parallel processing")
    
    from app.tasks import process_unit
    log.info(f"[PROCESS_DOC] Dispatching {len(units)} units to Celery")
    print(f"   Sending {len(units)} work units to processing queue...")
    
    for unit in units:
        try:
            process_unit.delay(document_id, session_id, unit.id)
            log.debug(f"[PROCESS_DOC] Unit {unit.id} dispatched")
            print(f"[PROCESS_DOC] Unit {unit.id} dispatched")
        except Exception:
            log.exception("Queue publish failed; queued-job recovery will republish %s", unit.id)
            print(f"[PROCESS_DOC] ERROR: Queue publish failed for unit {unit.id}")
    
    finalize(session_id)


def fail_document_setup(document_id, session_id, code, message):
    log.info(f"[FAIL_DOC_SETUP] Failing document setup for document_id={document_id}, session_id={session_id}, code={code}")
    print(f"[FAIL_DOC_SETUP] Failing document setup for document_id={document_id}, session_id={session_id}, code={code}")
    
    # Convert to UUID if string
    if isinstance(document_id, str):
        document_id = uuid.UUID(document_id)
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    
    with SessionLocal.begin() as db:
        doc = db.get(Document, document_id, with_for_update=True)
        session = db.get(ExtractionSession, session_id, with_for_update=True)
        
        if not doc or not session or doc.current_session_id != session.id or session.status in ("failed", "completed"):
            log.warning(f"[FAIL_DOC_SETUP] Document/session already failed or completed, skipping")
            print(f"[FAIL_DOC_SETUP] Document/session already failed or completed, skipping")
            return
        
        session.status = "failed"
        session.completed_at = now()
        session.error_code, session.error_message = code, message
        session.processing_time_ms = int((session.completed_at - (session.started_at or session.created_at)).total_seconds() * 1000)
        doc.status = "retry" if doc.attempt_count < doc.max_attempts else "failed"
        doc.next_retry_at = now() + timedelta(seconds=config.RETRY_BASE_SECONDS * 2 ** (doc.attempt_count - 1)) if doc.status == "retry" else None
        doc.processing_completed_at = now()
        doc.last_error_code, doc.last_error = code, message
        event(db, "SESSION_FAILED", doc.id, session.id, message=message, details={"code": code})
        
        log.info(f"[FAIL_DOC_SETUP] Document {document_id} marked as {doc.status}, session {session_id} failed")
        print(f"[FAIL_DOC_SETUP] Document {document_id} marked as {doc.status}, session {session_id} failed")


def dispatch_units(limit=100):
    log.info(f"[DISPATCH_UNITS] Starting unit dispatch, limit={limit}")
    print(f"[DISPATCH_UNITS] Starting unit dispatch, limit={limit}")
    
    ids = []
    with SessionLocal.begin() as db:
        units = db.scalars(select(ExtractionUnit).where(or_(ExtractionUnit.status == "pending", (ExtractionUnit.status == "retry") & (ExtractionUnit.next_retry_at <= now()))).order_by(ExtractionUnit.created_at, ExtractionUnit.unit_number).with_for_update(skip_locked=True).limit(limit)).all()
        
        log.info(f"[DISPATCH_UNITS] Found {len(units)} units to dispatch")
        print(f"[DISPATCH_UNITS] Found {len(units)} units to dispatch")
        
        for unit in units:
            session = db.get(ExtractionSession, unit.session_id)
            doc = db.get(Document, session.document_id)
            
            if session.status != "processing" or doc.current_session_id != session.id:
                log.debug(f"[DISPATCH_UNITS] Unit {unit.id} skipped - session not processing or document mismatch")
                print(f"[DISPATCH_UNITS] Unit {unit.id} skipped - session not processing or document mismatch")
                continue
            
            unit.status = "queued"
            unit.queued_at = now()
            unit.next_retry_at = None
            ids.append((str(doc.id), str(session.id), str(unit.id)))
            
            log.debug(f"[DISPATCH_UNITS] Unit {unit.id} queued for dispatch")
            print(f"[DISPATCH_UNITS] Unit {unit.id} queued for dispatch")
    
    from app.tasks import process_unit
    log.info(f"[DISPATCH_UNITS] Dispatching {len(ids)} units to Celery")
    print(f"[DISPATCH_UNITS] Dispatching {len(ids)} units to Celery")
    
    for document_id, session_id, unit_id in ids:
        try:
            process_unit.delay(document_id, session_id, unit_id)
            log.debug(f"[DISPATCH_UNITS] Unit {unit_id} dispatched")
            print(f"[DISPATCH_UNITS] Unit {unit_id} dispatched")
        except Exception:
            log.exception("Queue publish failed; queued-job recovery will republish %s", unit_id)
            print(f"[DISPATCH_UNITS] ERROR: Queue publish failed for unit {unit_id}")
    
    log.info(f"[DISPATCH_UNITS] Dispatch complete, {len(ids)} units dispatched")
    print(f"[DISPATCH_UNITS] Dispatch complete, {len(ids)} units dispatched")
    return len(ids)


def heartbeat(stop, document_id, session_id, unit_id, claim_id):
    log.debug(f"[HEARTBEAT] Starting heartbeat thread for unit_id={unit_id}, claim_id={claim_id}")
    print(f"[HEARTBEAT] Starting heartbeat thread for unit_id={unit_id}, claim_id={claim_id}")
    
    # Convert to UUID if string
    if isinstance(unit_id, str):
        unit_id = uuid.UUID(unit_id)
    
    while not stop.wait(15):
        try:
            with SessionLocal.begin() as db:
                unit = db.get(ExtractionUnit, unit_id)
                if not unit or unit.status != "processing" or unit.worker_id != claim_id:
                    log.debug(f"[HEARTBEAT] Unit {unit_id} no longer processing or worker changed, stopping heartbeat")
                    print(f"[HEARTBEAT] Unit {unit_id} no longer processing or worker changed, stopping heartbeat")
                    return
                unit.heartbeat_at = now()
                # Convert to UUID if string
                sess_id = uuid.UUID(session_id) if isinstance(session_id, str) else session_id
                doc_id = uuid.UUID(document_id) if isinstance(document_id, str) else document_id
                db.execute(update(ExtractionSession).where(ExtractionSession.id == sess_id).values(heartbeat_at=now()))
                db.execute(update(Document).where(Document.id == doc_id).values(heartbeat_at=now()))
                log.debug(f"[HEARTBEAT] Heartbeat updated for unit {unit_id}")
                print(f"[HEARTBEAT] Heartbeat updated for unit {unit_id}")
        except Exception:
            log.exception("Heartbeat failed for %s", unit_id)
            print(f"[HEARTBEAT] ERROR: Heartbeat failed for unit {unit_id}")


def process_unit_job(document_id, session_id, unit_id):
    log.info(f"[PROCESS_UNIT] Starting unit job for document_id={document_id}, session_id={session_id}, unit_id={unit_id}")
    print(f"🔍 Extracting data from PDF pages {unit_id}...")
    
    # Convert to UUID if string
    if isinstance(document_id, str):
        document_id = uuid.UUID(document_id)
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    if isinstance(unit_id, str):
        unit_id = uuid.UUID(unit_id)
    
    claim_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    log.info(f"[PROCESS_UNIT] Claim ID: {claim_id}")
    print(f"[PROCESS_UNIT] Claim ID: {claim_id}")
    
    with SessionLocal.begin() as db:
        unit = db.get(ExtractionUnit, unit_id, with_for_update=True)
        session = db.get(ExtractionSession, session_id)
        doc = db.get(Document, document_id)
        
        if not unit or not session or not doc or unit.session_id != session.id or session.document_id != doc.id or doc.current_session_id != session.id or unit.status != "queued":
            log.warning(f"[PROCESS_UNIT] Unit {unit_id} not in expected state, skipping")
            print(f"[PROCESS_UNIT] Unit {unit_id} not in expected state, skipping")
            return
        
        log.info(f"[PROCESS_UNIT] Unit {unit_id} validated, setting to processing")
        print(f"   Processing pages {unit.page_from} to {unit.page_to}...")
        
        unit.status = "processing"
        unit.attempt_count += 1
        unit.worker_id = claim_id
        unit.started_at = now()
        unit.heartbeat_at = now()
        event(db, "UNIT_STARTED", doc.id, session.id, unit.id, details={"attempt": unit.attempt_count})
        location, page_from, page_to = doc.source_path, unit.page_from, unit.page_to
        
        log.info(f"[PROCESS_UNIT] Unit {unit_id}: attempt={unit.attempt_count}, pages={page_from}-{page_to}, location={location}")
        print(f"   Attempt {unit.attempt_count}: Extracting pages {page_from}-{page_to}")
    
    stop = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(stop, document_id, session_id, unit_id, claim_id), daemon=True)
    thread.start()
    log.info(f"[PROCESS_UNIT] Heartbeat thread started for unit {unit_id}")
    print(f"   Monitoring extraction progress...")
    
    try:
        with SessionLocal() as db:
            log.info(f"[PROCESS_UNIT] Checking for cached raw response for unit {unit_id}")
            print(f"   Checking if data already extracted...")
            
            unit_id_uuid = uuid.UUID(unit_id) if isinstance(unit_id, str) else unit_id
            raw = db.scalar(select(RawResponse).where(RawResponse.extraction_unit_id == unit_id_uuid).order_by(RawResponse.id.desc()).limit(1))
            data = None
            
            if raw:
                log.info(f"[PROCESS_UNIT] Found cached raw response, attempting to validate")
                print(f"[PROCESS_UNIT] Found cached raw response, attempting to validate")
                try:
                    # Reuse successful extraction after a normalization/DB failure.
                    # An error or malformed payload must be re-extracted on retry.
                    data = validate_response(raw.raw_response, page_from, page_to)
                    log.info(f"[PROCESS_UNIT] Cached raw response validated successfully, reusing")
                    print(f"[PROCESS_UNIT] Cached raw response validated successfully, reusing")
                except ExtractionError:
                    log.info(f"[PROCESS_UNIT] Cached raw response invalid, will re-extract")
                    print(f"[PROCESS_UNIT] Cached raw response invalid, will re-extract")
        
        if data is None:
            log.info(f"[PROCESS_UNIT] No valid cached response, calling extraction service")
            print(f"   Calling OCR service to extract text from PDF...")
            
            data = extract(document_id, location, page_from, page_to)
            
            # Raw output is committed separately and before any normalization.
            log.info(f"[PROCESS_UNIT] Saving raw response to database")
            print(f"   Saving extracted data...")
            
            with SessionLocal.begin() as db:
                # Convert to UUID if string
                sess_id = uuid.UUID(session_id) if isinstance(session_id, str) else session_id
                unit_id_uuid = uuid.UUID(unit_id) if isinstance(unit_id, str) else unit_id
                doc_id_uuid = uuid.UUID(document_id) if isinstance(document_id, str) else document_id
                db.add(RawResponse(session_id=sess_id, extraction_unit_id=unit_id_uuid, raw_response=data, extractor_version=data.get("extractor_version") if isinstance(data, dict) else None))
                event(db, "RAW_RESPONSE_SAVED", doc_id_uuid, sess_id, unit_id_uuid)
                log.info(f"[PROCESS_UNIT] Raw response saved for unit {unit_id}")
                print(f"[PROCESS_UNIT] Raw response saved for unit {unit_id}")
        
        log.info(f"[PROCESS_UNIT] Validating extracted data")
        print(f"   Validating extracted data...")
        
        data = validate_response(data, page_from, page_to)
        
        log.info(f"[PROCESS_UNIT] Starting normalization for unit {unit_id}")
        print(f"   Converting extracted data to voter records...")
        
        normalize_unit(document_id, session_id, unit_id, data, claim_id)
        
        log.info(f"[PROCESS_UNIT] Unit {unit_id} processing complete")
        print(f"[PROCESS_UNIT] Unit {unit_id} processing complete")
        
    except Exception as exc:
        code = exc.code if isinstance(exc, ExtractionError) else ("NORMALIZATION_FAILED" if isinstance(exc, (ValueError, TypeError, KeyError)) else "DATABASE_ERROR")
        log.exception(f"[PROCESS_UNIT] Unit {unit_id} failed with code={code}")
        print(f"[PROCESS_UNIT] Unit {unit_id} failed with code={code}, error={str(exc)}")
        fail_unit(document_id, session_id, unit_id, code, str(exc), claim_id, getattr(exc, "retryable", True))
    finally:
        log.info(f"[PROCESS_UNIT] Stopping heartbeat thread for unit {unit_id}")
        print(f"   Extraction complete for this section")
        stop.set()
        thread.join(timeout=2)
    
    finalize(session_id)


def normalize_unit(document_id, session_id, unit_id, data, claim_id):
    log.info(f"[NORMALIZE_UNIT] Starting normalization for document_id={document_id}, session_id={session_id}, unit_id={unit_id}")
    print(f"📋 Processing extracted data from PDF pages...")
    
    # Convert to UUID if string
    doc_uuid = uuid.UUID(document_id) if isinstance(document_id, str) else document_id
    session_uuid = uuid.UUID(session_id) if isinstance(session_id, str) else session_id
    unit_uuid = uuid.UUID(unit_id) if isinstance(unit_id, str) else unit_id
    
    log.info(f"[NORMALIZE_UNIT] Mapping {len(data.get('records', []))} records")
    record_count = len(data.get('records', []))
    print(f"   Found {record_count} voter records to process")
    
    records = [map_record(item, doc_uuid, session_uuid, unit_uuid) for item in data["records"]]
    
    log.info(f"[NORMALIZE_UNIT] Checking for duplicate page/row identities")
    print(f"   Checking for duplicate records...")
    
    if len({(r.page_number, r.source_row_number) for r in records}) != len(records):
        log.error(f"[NORMALIZE_UNIT] Duplicate page/row identity found in extraction response")
        print(f"   ❌ Error: Found duplicate records in data")
        raise ValueError("Duplicate page/row identity in extraction response")
    
    log.info(f"[NORMALIZE_UNIT] Mapping document metadata")
    print(f"   Extracting document information (state, district, polling station...)")
    
    metadata = map_metadata(data, doc_uuid, session_uuid)
    
    log.info(f"[NORMALIZE_UNIT] Saving normalized data to database")
    print(f"   Saving voter records to database...")
    
    with SessionLocal.begin() as db:
        unit = db.get(ExtractionUnit, unit_uuid, with_for_update=True)
        
        if unit.status != "processing" or unit.worker_id != claim_id:
            log.warning(f"[NORMALIZE_UNIT] Unit {unit_id} state changed during normalization, aborting")
            print(f"[NORMALIZE_UNIT] Unit {unit_id} state changed during normalization, aborting")
            return
        
        event(db, "NORMALIZATION_STARTED", doc_uuid, session_uuid, unit_uuid)
        
        log.info(f"[NORMALIZE_UNIT] Unit {unit_id} is unit_number={unit.unit_number}")
        print(f"[NORMALIZE_UNIT] Unit {unit_id} is unit_number={unit.unit_number}")
        
        if unit.unit_number == 1:
            log.info(f"[NORMALIZE_UNIT] Saving document metadata (first unit)")
            print(f"   Saving document details (state, district, polling station)")
            values = {c.name: getattr(metadata, c.key) for c in ElectoralDocumentMetadata.__table__.columns if c.name not in ("id", "created_at")}
            db.execute(insert(ElectoralDocumentMetadata).values(**values).on_conflict_do_nothing(index_elements=[ElectoralDocumentMetadata.session_id]))
        
        log.info(f"[NORMALIZE_UNIT] Saving {len(records)} electoral records")
        print(f"[NORMALIZE_UNIT] Saving {len(records)} electoral records")

        written = 0
        for record in records:
            values = {c.name: getattr(record, c.key) for c in ElectoralRecord.__table__.columns if c.name not in ("id", "created_at")}
            # RETURNING, not rowcount. `result.rowcount` reports 3 here whether or
            # not the rows were inserted: with ON CONFLICT DO NOTHING the driver
            # counts rows the statement *processed*, not rows that landed. That
            # was verified against the live database -- the re-run case returns
            # rowcount 3 for zero inserted rows, which is exactly the
            # over-reporting this change exists to remove.
            #
            # RETURNING yields only rows that were actually inserted, so counting
            # them is the count we actually want. It also costs one extra column
            # per row on a path that is already the slowest in the pipeline.
            inserted = db.execute(
                insert(ElectoralRecord)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[ElectoralRecord.session_id, ElectoralRecord.page_number, ElectoralRecord.source_row_number],
                    index_where=(ElectoralRecord.page_number.is_not(None) & ElectoralRecord.source_row_number.is_not(None)),
                )
                .returning(ElectoralRecord.id)
            ).all()
            written += len(inserted)

        # The count that gets stored is the count that actually reached the
        # table, not the number of rows we intended to write.
        #
        # on_conflict_do_nothing discards a conflicting row silently, so
        # len(records) overstates what was written whenever a re-normalisation
        # hits rows this attempt already has. records_extracted is what the unit
        # page and the dashboard read as "how many voters did this unit
        # produce", so an inflated number there is a wrong answer presented
        # confidently -- and a shortfall goes unnoticed entirely, because
        # nothing else in the system compares the two numbers.
        unit.status = "completed"
        unit.completed_at = now()
        unit.records_extracted = written
        unit.error_code = unit.error_message = None
        db.execute(update(ExtractionSession).where(ExtractionSession.id == session_uuid).values(extractor_version=data["extractor_version"]))
        event(db, "NORMALIZATION_COMPLETED", doc_uuid, session_uuid, unit_uuid, details={"records": written, "records_expected": len(records)})
        event(db, "UNIT_COMPLETED", doc_uuid, session_uuid, unit_uuid)

        if written != len(records):
            # Not fatal: on_conflict_do_nothing is there to make re-runs
            # idempotent, so a shortfall on a retry is expected behaviour. But it
            # must be visible, or a genuinely truncated extraction looks
            # identical to a clean one.
            message = f"{len(records) - written} of {len(records)} records conflicted with existing rows and were not written"
            log.warning(f"[NORMALIZE_UNIT] Unit {unit_id} wrote {written}/{len(records)} records: {message}")
            print(f"   ⚠️  Wrote {written}/{len(records)} voter records ({message})")
            event(db, "NORMALIZATION_SHORTFALL", doc_uuid, session_uuid, unit_uuid,
                  message=message, details={"expected": len(records), "written": written})
        else:
            log.info(f"[NORMALIZE_UNIT] Unit {unit_id} normalization complete, {written} records saved")
            print(f"   ✅ Successfully saved {written} voter records")


def fail_unit(document_id, session_id, unit_id, code, message, claim_id, retryable=True):
    log.info(f"[FAIL_UNIT] Failing unit for document_id={document_id}, session_id={session_id}, unit_id={unit_id}, code={code}, retryable={retryable}")
    print(f"[FAIL_UNIT] Failing unit for document_id={document_id}, session_id={session_id}, unit_id={unit_id}, code={code}, retryable={retryable}")
    
    # Convert to UUID if string
    if isinstance(unit_id, str):
        unit_id = uuid.UUID(unit_id)
    
    with SessionLocal.begin() as db:
        unit = db.get(ExtractionUnit, unit_id, with_for_update=True)
        
        if not unit or unit.status != "processing" or unit.worker_id != claim_id:
            log.warning(f"[FAIL_UNIT] Unit {unit_id} not in expected state, skipping")
            print(f"[FAIL_UNIT] Unit {unit_id} not in expected state, skipping")
            return
        
        unit.status = "retry" if retryable and unit.attempt_count < config.MAX_UNIT_ATTEMPTS else "failed"
        unit.next_retry_at = now() + timedelta(seconds=config.RETRY_BASE_SECONDS * 2 ** (unit.attempt_count - 1)) if unit.status == "retry" else None
        unit.error_code, unit.error_message = code, message
        
        log.info(f"[FAIL_UNIT] Unit {unit_id} marked as {unit.status}, attempt={unit.attempt_count}")
        print(f"[FAIL_UNIT] Unit {unit_id} marked as {unit.status}, attempt={unit.attempt_count}")
        
        # Convert to UUID if string
        doc_id = uuid.UUID(document_id) if isinstance(document_id, str) else document_id
        sess_id = uuid.UUID(session_id) if isinstance(session_id, str) else session_id
        event(db, "UNIT_RETRY" if unit.status == "retry" else "UNIT_FAILED", doc_id, sess_id, unit.id, message, {"code": code, "attempt": unit.attempt_count})
        if code == "NORMALIZATION_FAILED":
            event(db, "NORMALIZATION_FAILED", doc_id, sess_id, unit.id, message)


def finalize(session_id):
    log.info(f"[FINALIZE] Finalizing session {session_id}")
    print(f"[FINALIZE] Finalizing session {session_id}")
    
    # Convert to UUID if string
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    
    with SessionLocal.begin() as db:
        session = db.get(ExtractionSession, session_id, with_for_update=True)
        
        if not session or session.status != "processing":
            log.debug(f"[FINALIZE] Session {session_id} not in processing state, skipping")
            print(f"[FINALIZE] Session {session_id} not in processing state, skipping")
            return
        
        units = db.scalars(select(ExtractionUnit).where(ExtractionUnit.session_id == session_id)).all()
        
        if not units:
            log.warning(f"[FINALIZE] Session {session_id} has no units, skipping")
            print(f"[FINALIZE] Session {session_id} has no units, skipping")
            return
        
        log.info(f"[FINALIZE] Session {session_id} has {len(units)} units")
        print(f"[FINALIZE] Session {session_id} has {len(units)} units")
        
        session.pages_processed = sum(u.page_to - u.page_from + 1 for u in units if u.status == "completed")
        session.records_extracted = sum(u.records_extracted for u in units)
        session.records_failed = sum(1 for u in units if u.status == "failed")
        
        log.info(f"[FINALIZE] Session {session_id}: pages_processed={session.pages_processed}, records_extracted={session.records_extracted}, records_failed={session.records_failed}")
        print(f"[FINALIZE] Session {session_id}: pages_processed={session.pages_processed}, records_extracted={session.records_extracted}, records_failed={session.records_failed}")
        
        if any(u.status in ("pending", "queued", "processing", "retry") for u in units):
            log.info(f"[FINALIZE] Session {session_id} still has active units, not finalizing yet")
            print(f"[FINALIZE] Session {session_id} still has active units, not finalizing yet")
            return
        
        doc = db.get(Document, session.document_id, with_for_update=True)
        
        if doc.current_session_id != session.id:
            log.warning(f"[FINALIZE] Document current session changed, skipping finalization")
            print(f"[FINALIZE] Document current session changed, skipping finalization")
            return
        
        failed = [u for u in units if u.status == "failed"]
        session.status = "completed" if not failed else ("partial" if len(failed) < len(units) else "failed")
        session.completed_at = now()
        session.processing_time_ms = int((session.completed_at - (session.started_at or session.created_at)).total_seconds() * 1000)
        doc.status = "completed" if not failed else "failed"
        doc.processing_completed_at = now()
        
        if failed:
            session.error_code, session.error_message = failed[0].error_code, failed[0].error_message
            doc.last_error_code, doc.last_error = failed[0].error_code, failed[0].error_message
        else:
            doc.last_error_code = doc.last_error = None
        
        event(db, "SESSION_COMPLETED" if not failed else "SESSION_FAILED", doc.id, session.id, details={"failed_units": len(failed)})
        
        log.info(f"[FINALIZE] Session {session_id} finalized with status={session.status}, failed_units={len(failed)}")
        print(f"[FINALIZE] Session {session_id} finalized with status={session.status}, failed_units={len(failed)}")


def recover():
    """Beat repairs lost workers and broker delivery gaps without resetting good units."""
    cutoff_unit = now() - timedelta(seconds=config.UNIT_STALE_SECONDS)
    cutoff_doc = now() - timedelta(seconds=config.DOCUMENT_STALE_SECONDS)
    queued_cutoff = now() - timedelta(seconds=60)
    republish_docs, republish_units, finalize_ids = [], [], set()
    with SessionLocal.begin() as db:
        docs = db.scalars(select(Document).where(Document.status.in_(["queued", "processing"])).with_for_update(skip_locked=True)).all()
        for doc in docs:
            session = db.get(ExtractionSession, doc.current_session_id) if doc.current_session_id else None
            # A "queued" document whose session has done no work, published
            # longer ago than queued_cutoff, is a publish the broker dropped --
            # not one still in flight. Republish it.
            #
            # This branch required `doc.locked_at` to be non-NULL, and nothing in
            # the codebase ever wrote it, so the value was always NULL and the
            # condition was always False. A document whose message was lost
            # between publish and pickup stayed "queued" for good: no worker was
            # coming, and dispatch_documents() only selects "pending" and
            # "retry", so nothing would ever pick it up again.
            #
            # dispatch_documents() now stamps locked_at when it publishes, which
            # is what makes the comparison meaningful. The refresh below is what
            # the original code intended and it is kept: it moves the deadline
            # forward on each republish, so a document whose broker stays down
            # is retried once per beat rather than once per document per second.
            #
            # Duplicate messages are harmless. process_document_job() re-checks
            # `doc.status == "queued" and session.status == "created"` under
            # SELECT FOR UPDATE at :405 and returns if another worker got there
            # first, so only the first delivery processes the document.
            if (doc.status == "queued" and doc.locked_at and doc.locked_at < queued_cutoff
                    and session and session.status == "created"):
                doc.locked_at = now()
                republish_docs.append((str(doc.id), str(session.id)))
            elif (doc.status == "processing" and session and session.pages_total is None
                    and (session.heartbeat_at or session.started_at)
                    and (session.heartbeat_at or session.started_at) < cutoff_doc):
                # Lost document worker before unit creation: fail this session and retry the PDF.
                session.status = "abandoned"
                session.completed_at = now()
                session.error_code = "WORKER_LOST"
                doc.status = "retry" if doc.attempt_count < doc.max_attempts else "failed"
                doc.next_retry_at = now() if doc.status == "retry" else None
                doc.last_error_code, doc.last_error = "WORKER_LOST", "Document worker heartbeat expired"
                event(db, "WORKER_LOST", doc.id, session.id)
        units = db.scalars(select(ExtractionUnit).where(ExtractionUnit.status.in_(["queued", "processing"])).with_for_update(skip_locked=True)).all()
        # NOTE on the timestamp guards below. `(x or y) < cutoff` raises
        # TypeError when BOTH are NULL -- `None < datetime` has no ordering --
        # and this whole function runs in one SessionLocal.begin(), so a raise
        # anywhere rolls back every republish it had queued and the beat is lost
        # entirely. Both timestamps being NULL is not hypothetical: any unit
        # claimed before it stamps started_at has that shape, and four such rows
        # were sitting in the live table when this was found.
        #
        # The guard is deliberately `is truthy`, not `is not None`, so it also
        # drops the empty-string case a datetime column cannot really hold.
        #
        # These units are left alone rather than failed. A row with no timestamps
        # is a unit that never really started, and nothing here can tell whether
        # a worker is attached to it -- dispatch_units() may have claimed it in
        # this same beat. Failing it would be a guess that can kill live work.
        # It costs one row per deployment in a schema bug that should not recur.
        for unit in units:
            session = db.get(ExtractionSession, unit.session_id)
            if session.status != "processing":
                continue
            doc = db.get(Document, session.document_id)
            if unit.status == "queued" and unit.queued_at and unit.queued_at < queued_cutoff:
                unit.queued_at = now()
                republish_units.append((str(doc.id), str(session.id), str(unit.id)))
            elif (unit.status == "processing" and (unit.heartbeat_at or unit.started_at)
                    and (unit.heartbeat_at or unit.started_at) < cutoff_unit):
                unit.status = "retry" if unit.attempt_count < config.MAX_UNIT_ATTEMPTS else "failed"
                unit.next_retry_at = now() if unit.status == "retry" else None
                unit.error_code, unit.error_message = "WORKER_LOST", "Unit worker heartbeat expired"
                event(db, "WORKER_LOST", doc.id, session.id, unit.id)
                finalize_ids.add(str(session.id))
    from app.tasks import process_document, process_unit
    for args in republish_docs:
        process_document.delay(*args)
    for args in republish_units:
        process_unit.delay(*args)
    for sid in finalize_ids:
        finalize(sid)
    return len(republish_docs) + len(republish_units) + len(finalize_ids)


def retry_document(document_id):
    # Convert to UUID if string
    if isinstance(document_id, str):
        document_id = uuid.UUID(document_id)
    
    with SessionLocal.begin() as db:
        doc = db.get(Document, document_id, with_for_update=True)
        if not doc or doc.status not in ("failed", "cancelled"):
            return False
        doc.status = "retry"
        doc.next_retry_at = now()
        doc.max_attempts = max(doc.max_attempts, doc.attempt_count + 1)
        event(db, "MANUAL_DOCUMENT_RETRY", doc.id)
    dispatch_documents()
    return True


def retry_unit(unit_id):
    # Convert to UUID if string
    if isinstance(unit_id, str):
        unit_id = uuid.UUID(unit_id)
    
    with SessionLocal.begin() as db:
        unit = db.get(ExtractionUnit, unit_id, with_for_update=True)
        if not unit or unit.status != "failed":
            return False
        session = db.get(ExtractionSession, unit.session_id, with_for_update=True)
        doc = db.get(Document, session.document_id, with_for_update=True)
        if doc.current_session_id != session.id or session.status not in ("partial", "failed"):
            return False
        unit.status, unit.next_retry_at = "retry", now()
        session.status, session.completed_at = "processing", None
        doc.status, doc.processing_completed_at = "processing", None
        event(db, "MANUAL_UNIT_RETRY", doc.id, session.id, unit.id)
    dispatch_units()
    return True
