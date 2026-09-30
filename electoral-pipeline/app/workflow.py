"""Database-owned state transitions. Redis carries IDs only."""
import hashlib
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone

from pypdf import PdfReader
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app import config
from app.db import SessionLocal
from app.extractor import ExtractionError, extract, validate_response
from app.models import Document, ElectoralDocumentMetadata, ElectoralRecord, ExtractionSession, ExtractionUnit, ProcessingEvent, RawResponse, now
from app.normalize import map_metadata, map_record


log = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"

log.info(f"[WORKFLOW] Worker ID initialized: {WORKER_ID}")
print(f"[WORKFLOW] Worker ID initialized: {WORKER_ID}")


def event(db, kind, document_id=None, session_id=None, unit_id=None, message=None, details=None):
    log.info(f"[EVENT] {kind} - document_id={document_id}, session_id={session_id}, unit_id={unit_id}, message={message}")
    print(f"[EVENT] {kind} - document_id={document_id}, session_id={session_id}, unit_id={unit_id}, message={message}")
    db.add(ProcessingEvent(document_id=document_id, session_id=session_id, extraction_unit_id=unit_id, service="pipeline", event_type=kind, message=message, details=details))


def discover():
    """Find PDFs by content hash. Same filename with changed bytes is a new document."""
    log.info(f"[DISCOVER] Starting PDF discovery in {config.DOCUMENT_ROOT}")
    print(f"[DISCOVER] Starting PDF discovery in {config.DOCUMENT_ROOT}")
    
    root = config.DOCUMENT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    found = 0
    
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        
        log.debug(f"[DISCOVER] Processing file: {path.name}")
        print(f"[DISCOVER] Processing file: {path.name}")
        
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            stat = path.stat()
            values = dict(source_type="local", source_document_id=None, source_path=str(path.resolve()), file_name=path.name,
                          file_hash=digest.hexdigest(), file_size=stat.st_size, source_modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc), status="pending")
            
            log.info(f"[DISCOVER] File {path.name}: size={stat.st_size} bytes, hash={digest.hexdigest()[:16]}...")
            print(f"[DISCOVER] File {path.name}: size={stat.st_size} bytes, hash={digest.hexdigest()[:16]}...")
            
            with SessionLocal.begin() as db:
                result = db.execute(insert(Document).values(**values).on_conflict_do_nothing(index_elements=[Document.source_type, Document.file_hash], index_where=(Document.source_document_id.is_(None) & Document.file_hash.is_not(None))).returning(Document.id))
                document_id = result.scalar_one_or_none()
                if document_id:
                    event(db, "DOCUMENT_DISCOVERED", document_id, message=path.name)
                    found += 1
                    log.info(f"[DISCOVER] New document registered: {document_id} for file {path.name}")
                    print(f"[DISCOVER] New document registered: {document_id} for file {path.name}")
                else:
                    log.info(f"[DISCOVER] Document already exists for file {path.name}")
                    print(f"[DISCOVER] Document already exists for file {path.name}")
        except OSError:
            log.exception("Could not discover %s", path)
            print(f"[DISCOVER] ERROR: Could not discover {path}")
    
    log.info(f"[DISCOVER] Discovery complete, found {found} new documents")
    print(f"[DISCOVER] Discovery complete, found {found} new documents")
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
        
        session.pages_total = pages
        units = []
        # Voter cards start from page 3, skip pages 1-2
        start_page = 3 if pages >= 3 else 1
        for index, start in enumerate(range(start_page, pages + 1, config.PAGES_PER_UNIT), 1):
            unit = ExtractionUnit(session_id=session.id, unit_number=index, page_from=start, page_to=min(start + config.PAGES_PER_UNIT - 1, pages))
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
        
        for record in records:
            values = {c.name: getattr(record, c.key) for c in ElectoralRecord.__table__.columns if c.name not in ("id", "created_at")}
            db.execute(insert(ElectoralRecord).values(**values).on_conflict_do_nothing(index_elements=[ElectoralRecord.session_id, ElectoralRecord.page_number, ElectoralRecord.source_row_number], index_where=(ElectoralRecord.page_number.is_not(None) & ElectoralRecord.source_row_number.is_not(None))))
        
        unit.status = "completed"
        unit.completed_at = now()
        unit.records_extracted = len(records)
        unit.error_code = unit.error_message = None
        db.execute(update(ExtractionSession).where(ExtractionSession.id == session_uuid).values(extractor_version=data["extractor_version"]))
        event(db, "NORMALIZATION_COMPLETED", doc_uuid, session_uuid, unit_uuid, details={"records": len(records)})
        event(db, "UNIT_COMPLETED", doc_uuid, session_uuid, unit_uuid)
        
        log.info(f"[NORMALIZE_UNIT] Unit {unit_id} normalization complete, {len(records)} records saved")
        print(f"   ✅ Successfully saved {len(records)} voter records")


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
            if doc.status == "queued" and doc.locked_at and doc.locked_at < queued_cutoff and session and session.status == "created":
                doc.locked_at = now()
                republish_docs.append((str(doc.id), str(session.id)))
            elif doc.status == "processing" and session and session.pages_total is None and (session.heartbeat_at or session.started_at) < cutoff_doc:
                # Lost document worker before unit creation: fail this session and retry the PDF.
                session.status = "abandoned"
                session.completed_at = now()
                session.error_code = "WORKER_LOST"
                doc.status = "retry" if doc.attempt_count < doc.max_attempts else "failed"
                doc.next_retry_at = now() if doc.status == "retry" else None
                doc.last_error_code, doc.last_error = "WORKER_LOST", "Document worker heartbeat expired"
                event(db, "WORKER_LOST", doc.id, session.id)
        units = db.scalars(select(ExtractionUnit).where(ExtractionUnit.status.in_(["queued", "processing"])).with_for_update(skip_locked=True)).all()
        for unit in units:
            session = db.get(ExtractionSession, unit.session_id)
            if session.status != "processing":
                continue
            doc = db.get(Document, session.document_id)
            if unit.status == "queued" and unit.queued_at and unit.queued_at < queued_cutoff:
                unit.queued_at = now()
                republish_units.append((str(doc.id), str(session.id), str(unit.id)))
            elif unit.status == "processing" and (unit.heartbeat_at or unit.started_at) < cutoff_unit:
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
