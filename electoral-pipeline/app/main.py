import logging
import secrets
import uuid
from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.extractor import mock_extract
from app.models import Document, ElectoralRecord, ExtractionSession, ExtractionUnit, ProcessingEvent, RawResponse
from app.presentation import (
    describe_error,
    document_progress,
    document_records_summary,
    duration_summary,
    event_label,
    file_size_summary,
    friendly_error,
    has_devanagari,
    progress_summary,
    records_summary,
    short_id,
    short_time,
    status_label,
    status_tone,
    time_ago,
)
from app.workflow import discover, dispatch_documents, retry_document, retry_unit
from app.transliterate import gender_en


log = logging.getLogger(__name__)

log.info(f"[MAIN] Initializing FastAPI application")
print(f"[MAIN] Initializing FastAPI application")

app = FastAPI(title="Electoral PDF Pipeline", version="1.0.0")
import os
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
security = HTTPBasic()

# Registered once, globally, so every template can call them without an import.
# These only format values for display; they never change what is stored.
templates.env.globals.update(
    status_label=status_label,
    status_tone=status_tone,
    event_label=event_label,
    describe_error=describe_error,
    friendly_error=friendly_error,
    progress_summary=progress_summary,
    records_summary=records_summary,
    duration_summary=duration_summary,
    time_ago=time_ago,
    short_time=short_time,
    file_size_summary=file_size_summary,
    short_id=short_id,
    has_devanagari=has_devanagari,
    document_progress=document_progress,
    document_records_summary=document_records_summary,
)

log.info(f"[MAIN] FastAPI app initialized with title={app.title}, version={app.version}")
print(f"[MAIN] FastAPI app initialized with title={app.title}, version={app.version}")


def admin(credentials: HTTPBasicCredentials = Depends(security)):
    log.debug(f"[MAIN] Admin authentication attempt for username={credentials.username}")
    print(f"[MAIN] Admin authentication attempt for username={credentials.username}")
    
    if not (secrets.compare_digest(credentials.username, "admin") and secrets.compare_digest(credentials.password, config.ADMIN_TOKEN)):
        log.warning(f"[MAIN] Admin authentication failed for username={credentials.username}")
        print(f"[MAIN] Admin authentication failed for username={credentials.username}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    
    log.debug(f"[MAIN] Admin authentication successful for username={credentials.username}")
    print(f"[MAIN] Admin authentication successful for username={credentials.username}")


def parse_uuid(value: str):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(404, "Invalid ID")


def render(request, name, **context):
    return templates.TemplateResponse(request=request, name=name, context=context)


@app.get("/health")
def health():
    log.debug(f"[MAIN] Health check requested")
    print(f"[MAIN] Health check requested")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    log.info(f"[MAIN] Dashboard requested")
    print(f"[MAIN] Dashboard requested")
    
    counts = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    today = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
    discovered = db.scalar(select(func.count()).select_from(Document).where(Document.discovered_at >= today))
    processed = db.scalar(select(func.count()).select_from(Document).where(Document.processing_completed_at >= today))
    active = db.scalar(select(func.count()).select_from(ExtractionSession).where(ExtractionSession.status == "processing"))
    recent = db.scalars(select(Document).order_by(Document.discovered_at.desc()).limit(12)).all()
    events = db.scalars(select(ProcessingEvent).order_by(ProcessingEvent.id.desc()).limit(12)).all()
    
    log.info(f"[MAIN] Dashboard data: counts={counts}, discovered={discovered}, processed={processed}, active={active}")
    print(f"[MAIN] Dashboard data: counts={counts}, discovered={discovered}, processed={processed}, active={active}")
    
    return render(request, "dashboard.html", counts=counts, total=sum(counts.values()), discovered=discovered, processed=processed, active=active, recent=recent, events=events)


@app.get("/documents", response_class=HTMLResponse, dependencies=[Depends(admin)])
def documents(request: Request, status: str = "", filename: str = "", date_from: str = "", session_id: str = "", document_id: str = "", page: int = 1, db: Session = Depends(get_db)):
    log.info(f"[MAIN] Documents page requested with filters: status={status}, filename={filename}, date_from={date_from}, session_id={session_id}, document_id={document_id}, page={page}")
    print(f"[MAIN] Documents page requested with filters: status={status}, filename={filename}, date_from={date_from}, session_id={session_id}, document_id={document_id}, page={page}")
    
    query = select(Document)
    if status:
        query = query.where(Document.status == status)
    if filename:
        query = query.where(Document.file_name.ilike(f"%{filename}%"))
    if date_from:
        try:
            query = query.where(Document.discovered_at >= date.fromisoformat(date_from))
        except ValueError:
            log.error(f"[MAIN] Invalid date_from format: {date_from}")
            print(f"[MAIN] Invalid date_from format: {date_from}")
            raise HTTPException(400, "Invalid date_from")
    if session_id:
        query = query.where(Document.current_session_id == parse_uuid(session_id))
    if document_id:
        query = query.where(Document.id == parse_uuid(document_id))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Document.discovered_at.desc()).limit(50).offset((max(page, 1) - 1) * 50)).all()
    session_ids = [d.current_session_id for d in rows if d.current_session_id]
    sessions = {s.id: s for s in db.scalars(select(ExtractionSession).where(ExtractionSession.id.in_(session_ids))).all()} if session_ids else {}
    
    log.info(f"[MAIN] Returning {len(rows)} documents, total={total}")
    print(f"[MAIN] Returning {len(rows)} documents, total={total}")
    
    return render(request, "documents.html", rows=rows, sessions=sessions, total=total, page=page, filters=dict(status=status, filename=filename, date_from=date_from, session_id=session_id, document_id=document_id))


@app.get("/documents/{document_id}", response_class=HTMLResponse, dependencies=[Depends(admin)])
def document_detail(request: Request, document_id: str, db: Session = Depends(get_db)):
    log.info(f"[MAIN] Document detail requested for document_id={document_id}")
    print(f"[MAIN] Document detail requested for document_id={document_id}")
    
    doc = db.get(Document, parse_uuid(document_id))
    if not doc:
        log.warning(f"[MAIN] Document not found: {document_id}")
        print(f"[MAIN] Document not found: {document_id}")
        raise HTTPException(404)
    
    sessions = db.scalars(select(ExtractionSession).where(ExtractionSession.document_id == doc.id).order_by(ExtractionSession.attempt_number.desc())).all()
    events = db.scalars(select(ProcessingEvent).where(ProcessingEvent.document_id == doc.id).order_by(ProcessingEvent.id.desc()).limit(100)).all()

    # Voter records for this document, newest session first. The point of the
    # bilingual work is seeing names in Hindi and English, so the table belongs
    # on the document page rather than only three clicks away on a unit page.
    records = db.scalars(
        select(ElectoralRecord)
        .where(ElectoralRecord.document_id == doc.id)
        .order_by(ElectoralRecord.page_number, ElectoralRecord.source_row_number)
        .limit(100)
    ).all()
    total_records = db.scalar(
        select(func.count()).select_from(ElectoralRecord).where(ElectoralRecord.document_id == doc.id)
    ) or 0

    log.info(f"[MAIN] Returning document {document_id} with {len(sessions)} sessions, {len(events)} events, {len(records)}/{total_records} records")
    print(f"[MAIN] Returning document {document_id} with {len(sessions)} sessions, {len(events)} events, {len(records)}/{total_records} records")

    return render(request, "document.html", doc=doc, sessions=sessions, events=events, records=records, total_records=total_records)


@app.get("/sessions/{session_id}", response_class=HTMLResponse, dependencies=[Depends(admin)])
def session_detail(request: Request, session_id: str, db: Session = Depends(get_db)):
    log.info(f"[MAIN] Session detail requested for session_id={session_id}")
    print(f"[MAIN] Session detail requested for session_id={session_id}")
    
    session = db.get(ExtractionSession, parse_uuid(session_id))
    if not session:
        log.warning(f"[MAIN] Session not found: {session_id}")
        print(f"[MAIN] Session not found: {session_id}")
        raise HTTPException(404)
    
    units = db.scalars(select(ExtractionUnit).where(ExtractionUnit.session_id == session.id).order_by(ExtractionUnit.unit_number)).all()
    
    log.info(f"[MAIN] Returning session {session_id} with {len(units)} units")
    print(f"[MAIN] Returning session {session_id} with {len(units)} units")
    
    return render(request, "session.html", session=session, doc=db.get(Document, session.document_id), units=units)


@app.get("/units/{unit_id}", response_class=HTMLResponse, dependencies=[Depends(admin)])
def unit_detail(request: Request, unit_id: str, db: Session = Depends(get_db)):
    log.info(f"[MAIN] Unit detail requested for unit_id={unit_id}")
    print(f"[MAIN] Unit detail requested for unit_id={unit_id}")
    
    unit = db.get(ExtractionUnit, parse_uuid(unit_id))
    if not unit:
        log.warning(f"[MAIN] Unit not found: {unit_id}")
        print(f"[MAIN] Unit not found: {unit_id}")
        raise HTTPException(404)
    
    session = db.get(ExtractionSession, unit.session_id)
    raw = db.scalars(select(RawResponse).where(RawResponse.extraction_unit_id == unit.id).order_by(RawResponse.id.desc())).all()
    records = db.scalars(select(ElectoralRecord).where(ElectoralRecord.extraction_unit_id == unit.id).order_by(ElectoralRecord.page_number, ElectoralRecord.source_row_number).limit(100)).all()
    events = db.scalars(select(ProcessingEvent).where(ProcessingEvent.extraction_unit_id == unit.id).order_by(ProcessingEvent.id.desc())).all()
    
    log.info(f"[MAIN] Returning unit {unit_id} with {len(raw)} raw responses, {len(records)} records, {len(events)} events")
    print(f"[MAIN] Returning unit {unit_id} with {len(raw)} raw responses, {len(records)} records, {len(events)} events")
    
    return render(request, "unit.html", unit=unit, session=session, raw=raw, records=records, events=events)


@app.post("/actions/discover", dependencies=[Depends(admin)])
def discover_now():
    log.info(f"[MAIN] Manual discover action triggered")
    print(f"[MAIN] Manual discover action triggered")
    
    discover()
    dispatch_documents()
    
    log.info(f"[MAIN] Discover and dispatch completed, redirecting to documents")
    print(f"[MAIN] Discover and dispatch completed, redirecting to documents")
    
    return RedirectResponse("/documents", status_code=303)


@app.post("/documents/{document_id}/retry", dependencies=[Depends(admin)])
def retry_document_action(document_id: str):
    log.info(f"[MAIN] Retry document action triggered for document_id={document_id}")
    print(f"[MAIN] Retry document action triggered for document_id={document_id}")
    
    if not retry_document(parse_uuid(document_id)):
        log.warning(f"[MAIN] Retry failed for document {document_id} - only failed/cancelled documents can be retried")
        print(f"[MAIN] Retry failed for document {document_id} - only failed/cancelled documents can be retried")
        raise HTTPException(409, "Only failed/cancelled documents can be retried")
    
    log.info(f"[MAIN] Document {document_id} retry successful, redirecting")
    print(f"[MAIN] Document {document_id} retry successful, redirecting")
    
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@app.post("/units/{unit_id}/retry", dependencies=[Depends(admin)])
def retry_unit_action(unit_id: str):
    log.info(f"[MAIN] Retry unit action triggered for unit_id={unit_id}")
    print(f"[MAIN] Retry unit action triggered for unit_id={unit_id}")
    
    if not retry_unit(parse_uuid(unit_id)):
        log.warning(f"[MAIN] Retry failed for unit {unit_id} - only failed units in current failed/partial session can be retried")
        print(f"[MAIN] Retry failed for unit {unit_id} - only failed units in current failed/partial session can be retried")
        raise HTTPException(409, "Only a failed unit in the current failed/partial session can be retried")
    
    log.info(f"[MAIN] Unit {unit_id} retry successful, redirecting")
    print(f"[MAIN] Unit {unit_id} retry successful, redirecting")
    
    return RedirectResponse(f"/units/{unit_id}", status_code=303)


@app.post("/mock-extract", dependencies=[Depends(admin)])
async def mock_endpoint(request: Request):
    log.info(f"[MAIN] Mock extract endpoint called")
    print(f"[MAIN] Mock extract endpoint called")

    body = await request.json()

    log.debug(f"[MAIN] Mock extract request body: page_from={body.get('page_from')}, page_to={body.get('page_to')}")
    print(f"[MAIN] Mock extract request body: page_from={body.get('page_from')}, page_to={body.get('page_to')}")

    if not isinstance(body.get("page_from"), int) or not isinstance(body.get("page_to"), int) or body["page_from"] < 1 or body["page_to"] < body["page_from"]:
        log.error(f"[MAIN] Invalid page range in mock extract request")
        print(f"[MAIN] Invalid page range in mock extract request")
        raise HTTPException(400, "Invalid page range")

    result = mock_extract(body)

    log.info(f"[MAIN] Mock extract completed successfully")
    print(f"[MAIN] Mock extract completed successfully")

    return result


@app.get("/documents/{document_id}/export", dependencies=[Depends(admin)])
def export_csv(document_id: str, db: Session = Depends(get_db)):
    """Export voter records as CSV."""
    import csv
    import io
    import json
    from fastapi.responses import StreamingResponse
    from sqlalchemy import select
    from urllib.parse import quote

    from app.models import ElectoralRecord

    doc = db.get(Document, parse_uuid(document_id))
    if not doc:
        raise HTTPException(404)

    # Get all records for this document
    records = db.scalars(
        select(ElectoralRecord)
        .where(ElectoralRecord.document_id == doc.id)
        .order_by(ElectoralRecord.page_number, ElectoralRecord.source_row_number)
    ).all()

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)

        # Write header with all fields
        headers = [
            'sno', 'page_number', 'card_index', 'voter_sr_no', 'id_card_no',
            'gender', 'age', 'house_no',
            'voter_first_name', 'voter_middle_name', 'voter_sur_name',
            'relation_name',
            'voter_father_first_name', 'voter_father_middle_name', 'voter_father_last_name',
            'voter_husband_first_name', 'voter_husband_middle_name', 'voter_husband_last_name',
            'voter_mother_first_name', 'voter_mother_middle_name', 'voter_mother_last_name',
            'voter_other_first_name', 'voter_other_middle_name', 'voter_other_last_name',
            'is_deleted',
            'state_code', 'ac_code', 'anubhag_code', 'anubhag_name', 'booth_code', 'pdf_name',
            'needs_review', 'review_reasons', 'field_sources',
            # Existing fields for compatibility
            'serial_number', 'epic_number',
            'name_hi', 'name_en',
            'relative_name_hi', 'relative_name_en',
            'relationship_type',
            'gender_hi', 'gender_en',
            'house_number_hi', 'house_number_en',
            'section_number', 'section_name_hi', 'section_name_en',
            'confidence', 'source_record_hash', 'translit_source_hash',
            'is_valid', 'validation_errors'
        ]
        writer.writerow(headers)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for r in records:
            # Convert JSONB fields to JSON strings
            review_reasons_str = json.dumps(r.review_reasons) if r.review_reasons else ''
            field_sources_str = json.dumps(r.field_sources) if r.field_sources else ''
            
            row = [
                r.sno or '',
                r.page_number or '',
                r.card_index or '',
                r.voter_sr_no or '',
                r.epic_number or '',  # id_card_no = epic_number
                r.gender_hi or '',    # gender = gender_hi
                r.age or '',
                r.house_number_hi or '',  # house_no = house_number_hi
                r.voter_first_name_hi or '',
                r.voter_middle_name_hi or '',
                r.voter_sur_name_hi or '',
                r.relation_name or '',
                r.voter_father_first_name_hi or '',
                r.voter_father_middle_name_hi or '',
                r.voter_father_last_name_hi or '',
                r.voter_husband_first_name_hi or '',
                r.voter_husband_middle_name_hi or '',
                r.voter_husband_last_name_hi or '',
                r.voter_mother_first_name_hi or '',
                r.voter_mother_middle_name_hi or '',
                r.voter_mother_last_name_hi or '',
                r.voter_other_first_name_hi or '',
                r.voter_other_middle_name_hi or '',
                r.voter_other_last_name_hi or '',
                r.is_deleted if r.is_deleted is not None else '',
                r.state_code or '',
                r.ac_code or '',
                r.anubhag_code or '',
                r.anubhag_name or '',
                r.booth_code or '',
                r.pdf_name or '',
                r.needs_review if r.needs_review is not None else '',
                review_reasons_str,
                field_sources_str,
                r.serial_number or '',
                r.epic_number or '',
                r.name_hi or '',
                r.name_en or '',
                r.relative_name_hi or '',
                r.relative_name_en or '',
                r.relationship_type or '',
                r.gender_hi or '',
                r.gender_en or '',
                r.house_number_hi or '',
                r.house_number_en or '',
                r.section_number or '',
                r.section_name_hi or '',
                r.section_name_en or '',
                r.confidence or '',
                r.source_record_hash or '',
                r.translit_source_hash or '',
                r.is_valid if r.is_valid is not None else '',
                r.validation_errors or '',
            ]
            writer.writerow(row)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    # Clean filename and ensure proper encoding
    safe_filename = doc.file_name.replace('.pdf', '') + '_export.csv'
    encoded_filename = quote(safe_filename)
    
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )


@app.get("/sessions/{session_id}/status", dependencies=[Depends(admin)])
def session_status(session_id: str, db: Session = Depends(get_db)):
    """Get live status for a session (for polling)."""
    session = db.get(ExtractionSession, parse_uuid(session_id))
    if not session:
        raise HTTPException(404)

    units = db.scalars(select(ExtractionUnit).where(ExtractionUnit.session_id == session.id).order_by(ExtractionUnit.unit_number)).all()

    return {
        "status": session.status,
        "pages_processed": session.pages_processed,
        "pages_total": session.pages_total,
        "records_extracted": session.records_extracted,
        "records_failed": session.records_failed,
        "processing_time_ms": session.processing_time_ms,
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        "units": [
            {
                "id": str(u.id),
                "status": u.status,
                "records_extracted": u.records_extracted,
                "error_code": u.error_code,
                "error_message": u.error_message,
            }
            for u in units
        ],
    }


@app.get("/browse", response_class=HTMLResponse, dependencies=[Depends(admin)])
def browse_records(
    request: Request,
    table: str = "",
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Read-only browser over every table in the database.

    Which table and which columns are shown comes from app.db_browse, never
    from the request: a name that is not in the registry is refused, so a
    visitor cannot reach anything the pipeline does not already expose.
    """
    from sqlalchemy import func, select, text
    from app.db_browse import (
        TABLE_ORDER,
        TABLES,
        Table,
        cell_text,
        columns_present_in,
        default_table,
        get_table,
    )

    chosen = get_table(table) or default_table()
    PER_PAGE = 50
    page = max(1, page)

    # Read the live column list and drop any registry entry the database does
    # not have. A renamed column should cost one column, not the whole page.
    actual = {r[0] for r in db.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_name = :t"), {"t": chosen.name})}
    missing = columns_present_in(chosen, actual)
    if missing:
        chosen = Table(
            name=chosen.name,
            label=chosen.label,
            description=chosen.description,
            columns=[c for c in chosen.columns if c.name not in missing],
        )

    # Row counts for every table, so the tab list can show what is in each.
    counts: dict[str, int] = {}
    for name in TABLE_ORDER:
        try:
            counts[name] = db.scalar(
                text(f"SELECT count(*) FROM {name}")  # name from registry only
            ) or 0
        except Exception:
            counts[name] = 0

    # The whole query runs as one statement built from registry column names.
    # No part of it comes from user input: the only user value is the search
    # term, which is passed as a bound parameter.
    col_names = [c.name for c in chosen.columns]
    select_list = ", ".join(col_names)
    params: dict[str, object] = {}

    where = ""
    if q.strip():
        # Search the text columns of this table. Bound as a parameter, so the
        # term is data and can never be read as SQL.
        searchable = [c.name for c in chosen.columns
                      if not c.numeric and not c.expandable]
        if searchable:
            clause = " OR ".join(f'"{n}"::text ILIKE :term' for n in searchable)
            where = f" WHERE {clause}"
            params["term"] = f"%{q.strip()}%"

    total_sql = f"SELECT count(*) FROM {chosen.name}{where}"
    total = db.scalar(text(total_sql), params) or 0

    # Newest rows first for the log-like tables, page order for the voter list.
    if chosen.name == "electoral_records":
        order = '"page_number" NULLS LAST, "source_row_number" NULLS LAST, id'
    elif chosen.name == "processing_events":
        order = '"created_at" DESC NULLS LAST, id DESC'
    elif chosen.name == "extraction_raw_responses":
        order = '"received_at" DESC NULLS LAST, id DESC'
    else:
        order = "id"

    rows_sql = (
        f"SELECT {select_list} FROM {chosen.name}{where} "
        f"ORDER BY {order} LIMIT :lim OFFSET :off"
    )
    params.update(lim=PER_PAGE, off=(page - 1) * PER_PAGE)
    fetched = db.execute(text(rows_sql), params).fetchall()

    # Transpose rows into per-column lists so the template can loop columns on
    # the outside, which keeps the header and body in step.
    cells = []
    for row in fetched:
        cells.append({c.name: row[idx] for idx, c in enumerate(chosen.columns)})

    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return render(
        request,
        "browse.html",
        table=chosen,
        tables=[TABLES[n] for n in TABLE_ORDER],
        counts=counts,
        cells=cells,
        total=total,
        query=q,
        page=page,
        pages=pages,
        per_page=PER_PAGE,
        cell_text=cell_text,
    )


@app.get("/browse/export", dependencies=[Depends(admin)])
def browse_export(
    q: str = "",
    gender: str = "",
    age: str = "",
    lang: str = "en",
    db: Session = Depends(get_db),
):
    """Export all filtered records as CSV."""
    import csv
    import io
    from fastapi.responses import StreamingResponse
    from sqlalchemy import select, func, or_, and_

    # Build query (same filters as browse_records)
    query = select(ElectoralRecord)
    filters = []
    if q:
        q_clean = q.strip()
        search_term = f"%{q_clean}%"
        filters.append(
            or_(
                ElectoralRecord.name_hi.ilike(search_term),
                ElectoralRecord.name_en.ilike(search_term),
                ElectoralRecord.epic_number.ilike(search_term),
                ElectoralRecord.voter_first_name_hi.ilike(search_term),
                ElectoralRecord.voter_first_name_en.ilike(search_term),
                ElectoralRecord.voter_middle_name_hi.ilike(search_term),
                ElectoralRecord.voter_middle_name_en.ilike(search_term),
                ElectoralRecord.voter_sur_name_hi.ilike(search_term),
                ElectoralRecord.voter_sur_name_en.ilike(search_term),
                ElectoralRecord.relative_name_hi.ilike(search_term),
                ElectoralRecord.relative_name_en.ilike(search_term),
                ElectoralRecord.house_number_hi.ilike(search_term),
                ElectoralRecord.house_number_en.ilike(search_term),
            )
        )
    if gender:
        gender_en_val = gender_en(gender) or gender
        filters.append(
            or_(
                ElectoralRecord.gender_hi == gender,
                ElectoralRecord.gender_en == gender_en_val,
            )
        )
    if age:
        try:
            if "-" in age:
                min_age, max_age = map(int, age.split("-"))
                filters.append(and_(ElectoralRecord.age >= min_age, ElectoralRecord.age <= max_age))
            else:
                filters.append(ElectoralRecord.age == int(age))
        except ValueError:
            pass
    if filters:
        query = query.where(and_(*filters))

    records = db.scalars(query.order_by(ElectoralRecord.document_id, ElectoralRecord.page_number, ElectoralRecord.source_row_number)).all()

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)

        headers = [
            'sno', 'page_number', 'card_index', 'voter_sr_no', 'id_card_no',
            'gender', 'age', 'house_no',
            'voter_first_name', 'voter_middle_name', 'voter_sur_name',
            'relation_name',
            'voter_father_first_name', 'voter_father_middle_name', 'voter_father_last_name',
            'voter_husband_first_name', 'voter_husband_middle_name', 'voter_husband_last_name',
            'voter_mother_first_name', 'voter_mother_middle_name', 'voter_mother_last_name',
            'voter_other_first_name', 'voter_other_middle_name', 'voter_other_last_name',
            'is_deleted',
            'state_code', 'ac_code', 'anubhag_code', 'anubhag_name', 'booth_code', 'pdf_name',
            'needs_review', 'review_reasons', 'field_sources',
            'serial_number', 'epic_number',
            'name_hi', 'name_en',
            'relative_name_hi', 'relative_name_en',
            'relationship_type',
            'gender_hi', 'gender_en',
            'house_number_hi', 'house_number_en',
            'section_number', 'section_name_hi', 'section_name_en',
            'confidence', 'source_record_hash', 'translit_source_hash',
            'is_valid', 'validation_errors'
        ]
        writer.writerow(headers)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for r in records:
            row = [
                r.sno,
                r.page_number,
                r.card_index,
                r.voter_sr_no,
                r.epic_number,
                r.gender_hi,
                r.age,
                r.house_number_hi,
                r.voter_first_name_hi,
                r.voter_middle_name_hi,
                r.voter_sur_name_hi,
                r.relation_name,
                r.voter_father_first_name_hi,
                r.voter_father_middle_name_hi,
                r.voter_father_last_name_hi,
                r.voter_husband_first_name_hi,
                r.voter_husband_middle_name_hi,
                r.voter_husband_last_name_hi,
                r.voter_mother_first_name_hi,
                r.voter_mother_middle_name_hi,
                r.voter_mother_last_name_hi,
                r.voter_other_first_name_hi,
                r.voter_other_middle_name_hi,
                r.voter_other_last_name_hi,
                r.is_deleted,
                r.state_code,
                r.ac_code,
                r.anubhag_code,
                r.anubhag_name,
                r.booth_code,
                r.pdf_name,
                r.needs_review,
                r.review_reasons,
                r.field_sources,
                r.serial_number,
                r.epic_number,
                r.name_hi,
                r.name_en,
                r.relative_name_hi,
                r.relative_name_en,
                r.relationship_type,
                r.gender_hi,
                r.gender_en,
                r.house_number_hi,
                r.house_number_en,
                r.section_number,
                r.section_name_hi,
                r.section_name_en,
                r.confidence,
                r.source_record_hash,
                r.translit_source_hash,
                r.is_valid,
                r.validation_errors,
            ]
            writer.writerow(row)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=electoral_records_export.csv"}
    )
