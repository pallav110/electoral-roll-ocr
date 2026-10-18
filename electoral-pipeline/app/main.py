import logging
import re
import secrets
import base64
import hashlib
import hmac
import time
import uuid
import asyncio
import subprocess
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.sessions import SessionMiddleware
from starlette.background import BackgroundTask
from starlette.formparsers import MultiPartParser
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.orm import Session, selectinload

from app import config
from app.db import SessionLocal, get_db

# Stop before binding the port when MinIO is pointed at the public site.
# A wrong address does not fail quickly: the calls hang and the site looks down.
config.refuse_bad_minio_config("web")
from app.extractor import mock_extract
from app.models import AdminUser, ArchiveIngestion, Document, DocumentCover, DocumentLocation, DocumentVisualAsset, ElectoralRecord, ExtractionSession, ExtractionUnit, ProcessingEvent, RawResponse, ScanRoot, UploadBatch
from app.document_locations import cover_location_query, location_for_document
from app.map_verification import verifications_for_document
from app.visual_assets import assets_for_document, extract_visual_assets
from app.record_scope import batch_extraction_stats, count_records_for_document, records_for_document, session_gender_counts
from app.completeness_export import completeness_rows
from app.presentation import (
    duration_between,
    event_detail,
    describe_error,
    document_progress,
    document_records_summary,
    duration_summary,
    eta_summary,
    event_label,
    file_size_summary,
    friendly_error,
    fuse_progress,
    has_devanagari,
    live_progress,
    progress_summary,
    records_summary,
    short_id,
    short_time,
    status_label,
    status_tone,
    time_ago,
)
from app.workflow import (_resolve_scan_path, delete_documents, discover, discover_upload_batch,
                         dispatch_documents, retry_document, retry_unit,
                         select_documents_for_extraction)


def _dashboard_timing_metrics(documents, units, *, total_pages, completed_pages,
                              completed_documents, remaining_pages):
    """Return dashboard timing from document wall time and unit elapsed time.

    These are deliberately separate metrics. Unit spans can overlap across OCR
    readers, so summing units is useful for capacity analysis but is not a PDF's
    wall-clock duration. ETA is page-based because PDFs have very different page
    counts; documents-based ETA turns a short roll and a 41-page roll into the
    same unit of work.
    """
    completed_docs = [
        d for d in documents
        if getattr(d, "status", None) == "completed"
        and getattr(d, "processing_started_at", None)
        and getattr(d, "processing_completed_at", None)
    ]
    wall_seconds = [
        max(0.0, (d.processing_completed_at - d.processing_started_at).total_seconds())
        for d in completed_docs
    ]
    settled = [u for u in units if getattr(u, "status", None) == "completed"]
    unit_seconds = 0.0
    unit_pages = 0
    for u in settled:
        if not u.started_at or not u.completed_at:
            continue
        pages = max(0, int(u.page_to) - int(u.page_from) + 1)
        if pages:
            unit_pages += pages
            unit_seconds += max(0.0, (u.completed_at - u.started_at).total_seconds())
    seconds_per_page = unit_seconds / unit_pages if unit_pages else None
    pages_remaining = max(0, int(remaining_pages or 0))
    return {
        "avg_document_wall_seconds": (
            sum(wall_seconds) / len(wall_seconds) if wall_seconds else None
        ),
        "unit_seconds_per_page": seconds_per_page,
        "unit_elapsed_seconds": unit_seconds,
        "unit_pages_measured": unit_pages,
        "pages_remaining": pages_remaining,
        "eta_seconds": int(pages_remaining * seconds_per_page)
        if seconds_per_page is not None else None,
        "completed_pages": int(completed_pages or 0),
        "total_pages": int(total_pages or 0),
        "completed_documents": int(completed_documents or 0),
    }
from app.transliterate import gender_en
from app.api_auth import issue_api_token, user_from_bearer
from app.signoz_sso import SignozError, boot_url, login_tokens
from app.uploads import UploadValidationError, safe_batch_name, save_upload_batch
from app.archive_ingest import ensure_bucket, s3_client
from app.minio_proxy import SSO_PATH as MINIO_SSO_PATH, clear_console_cookie, router as minio_console_router
from app.db_browse import house_number_display


log = logging.getLogger(__name__)

log.info(f"[MAIN] Initializing FastAPI application")
print(f"[MAIN] Initializing FastAPI application")

app = FastAPI(title="Electoral PDF Pipeline", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    session_cookie="electoral_admin_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=config.SESSION_COOKIE_SECURE,
)


@app.middleware("http")
async def prevent_dynamic_page_caching(request: Request, call_next):
    """Never let a browser/proxy cache an authenticated page or its redirect.

    A cached unauthenticated 303 from ``/upload`` looks exactly like a broken
    login loop: after a successful sign-in the browser reuses its old
    ``/upload -> /login`` response instead of contacting the application.
    Upload, document and API responses all depend on the signed admin session
    and live database state, so they must always be fetched fresh.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        # The public proxy currently forces a long browser TTL.  A cache key
        # that varies by the signed admin cookie still prevents a pre-login
        # `/upload -> /login` redirect from being reused after sign-in.
        response.headers["Vary"] = "Cookie"
    return response


app.include_router(minio_console_router)

# Starlette caps a single multipart part at 1 MB (Request.form's own default,
# which FastAPI never raises) and rejects anything larger with a bare
# HTTPException 400 -- "There was an error parsing the body", naming neither the
# file nor the reason. Scanned electoral rolls are routinely 5-20 MB, so that
# default rejected the real use case outright while MAX_UPLOAD_FILE_MB
# advertised a 100 MB per-file limit that could never be reached.
#
# The per-file cap belongs to save_upload_batch: it knows the file's name and
# can say that nothing from the upload was saved, which is a message a user can
# act on. This is only the framework backstop for a genuinely unbounded body,
# so it is set at the batch ceiling -- one file may legitimately be as large as
# the per-file limit allows.
MultiPartParser.max_part_size = config.MAX_UPLOAD_FILE_MB * 1024 * 1024
import os
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
security = HTTPBasic(auto_error=False)


def signoz_available_locally(request: Request) -> bool:
    """Whether this request came through an explicitly local operator URL."""
    host = (request.url.hostname or "").strip().lower().strip("[]")
    return host in config.SIGNOZ_LOCAL_HOSTS

def event_detail(details, message=None):
    if details:
        if isinstance(details, dict):
            return " | ".join(f"{key}: {value}" for key, value in details.items())
        return str(details)
    return message or ""

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
    duration_between=duration_between,
    event_detail=event_detail,
    live_progress=live_progress,
    fuse_progress=fuse_progress,
    eta_summary=eta_summary,
    time_ago=time_ago,
    short_time=short_time,
    house_number_display=house_number_display,
    file_size_summary=file_size_summary,
    signoz_available_locally=signoz_available_locally,
    short_id=short_id,
    has_devanagari=has_devanagari,
    document_progress=document_progress,
    document_records_summary=document_records_summary,
)


def source_label(source_path: str) -> str:
    try:
        return str(Path(source_path).resolve().relative_to(config.DOCUMENT_ROOT))
    except (TypeError, ValueError):
        return source_path


templates.env.globals["source_label"] = source_label

log.info(f"[MAIN] FastAPI app initialized with title={app.title}, version={app.version}")
print(f"[MAIN] FastAPI app initialized with title={app.title}, version={app.version}")


def _hash_password(password: str) -> str:
    """Hash a human password with memory-hard scrypt; never store plaintext."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt.encode("ascii")),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected.encode("ascii")))
    except (ValueError, TypeError, KeyError):
        return False


def _ensure_bootstrap_admin(db: Session) -> None:
    """Create exactly one initial account when deployment secrets provide it."""
    if db.scalar(select(func.count()).select_from(AdminUser)):
        return
    if not config.BOOTSTRAP_ADMIN_USERNAME or not config.BOOTSTRAP_ADMIN_PASSWORD:
        raise RuntimeError("No administrator exists; set BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD.")
    db.add(AdminUser(
        username=config.BOOTSTRAP_ADMIN_USERNAME,
        password_hash=_hash_password(config.BOOTSTRAP_ADMIN_PASSWORD),
        is_superadmin=True,
    ))
    db.commit()
    log.warning("Created bootstrap administrator %s", config.BOOTSTRAP_ADMIN_USERNAME)


def _unauthorized(request: Request) -> HTTPException:
    """Pages go to the sign-in form. API calls get a 401 they can handle."""
    accepts_html = "text/html" in request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or not accepts_html:
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required.")
    requested = request.url.path
    if request.url.query:
        requested = f"{requested}?{request.url.query}"
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/login?" + urlencode({"next": requested})},
    )


def admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    _ensure_bootstrap_admin(db)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        user = user_from_bearer(db, header[7:])
        if user and user.is_active:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token.")
    raw_user_id = request.session.get("admin_user_id")
    if raw_user_id:
        try:
            user = db.get(AdminUser, uuid.UUID(raw_user_id))
        except (TypeError, ValueError):
            user = None
        if user and user.is_active:
            return user
    # Preserve the requested local path.  Sending every protected route to a
    # bare `/login` loses the intent, so a successful login lands at `/` and
    # makes `/upload` look like it keeps demanding a login.
    raise _unauthorized(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    return templates.TemplateResponse("login.html", {"request": request, "next": safe_next})


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    _ensure_bootstrap_admin(db)
    user = db.scalar(select(AdminUser).where(AdminUser.username == username.strip()))
    if not user or not user.is_active or not _password_matches(password, user.password_hash):
        log.warning("Login failed for username=%r", username[:128])
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": "/", "error": "Incorrect username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session.clear()
    request.session["admin_user_id"] = str(user.id)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    # The MinIO file manager was signed in on this person's behalf; end that too.
    clear_console_cookie(response)
    return response


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _require_superadmin(user: AdminUser = Depends(admin)) -> AdminUser:
    if not user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required.")
    return user


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    _user: AdminUser = Depends(_require_superadmin),
    db: Session = Depends(get_db),
):
    users = db.scalars(select(AdminUser).order_by(AdminUser.username)).all()
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "users": users,
        "csrf_token": _csrf_token(request),
    })


@app.post("/admin/users")
def create_admin_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    _user: AdminUser = Depends(_require_superadmin),
    db: Session = Depends(get_db),
):
    if not hmac.compare_digest(csrf_token, request.session.get("csrf_token", "")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid form token.")
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,128}", username):
        raise HTTPException(status_code=422, detail="Username must be 3-128 safe characters.")
    if len(password) < 14:
        raise HTTPException(status_code=422, detail="Password must be at least 14 characters.")
    if db.scalar(select(AdminUser.id).where(AdminUser.username == username)):
        raise HTTPException(status_code=409, detail="That username already exists.")
    db.add(AdminUser(username=username, password_hash=_hash_password(password)))
    db.commit()
    log.info("Administrator %s created user %s", _user.username, username)
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/users")
async def create_admin_user_api(
    request: Request,
    caller: AdminUser = Depends(_require_superadmin),
    db: Session = Depends(get_db),
):
    """Create an account and return its API token. The token is shown once."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Send a JSON body.")
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,128}", username):
        raise HTTPException(status_code=422, detail="Username must be 3-128 safe characters.")
    if len(password) < 14:
        raise HTTPException(status_code=422, detail="Password must be at least 14 characters.")
    if db.scalar(select(AdminUser.id).where(AdminUser.username == username)):
        raise HTTPException(status_code=409, detail="That username already exists.")
    user = AdminUser(
        username=username,
        password_hash=_hash_password(password),
        is_superadmin=bool(payload.get("is_superadmin")),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = issue_api_token(db, user)
    log.info("Administrator %s created API user %s", caller.username, username)
    return {
        "username": user.username,
        "is_superadmin": user.is_superadmin,
        "api_token": token,
        "usage": "Authorization: Bearer <api_token>",
    }


@app.get("/api/me")
def api_me(user: AdminUser = Depends(admin)):
    return {"username": user.username, "is_superadmin": user.is_superadmin}


@app.get("/signoz")
def open_signoz(request: Request, _user: AdminUser = Depends(admin)):
    """Send an already signed-in operator into SigNoz without a second login."""
    if not signoz_available_locally(request):
        # Do not disclose an internal network tool to the public deployment.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    try:
        access, refresh = login_tokens()
    except SignozError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="SigNoz is not reachable yet.")
    return RedirectResponse(boot_url(access, refresh), status_code=status.HTTP_303_SEE_OTHER)


def parse_uuid(value: str):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(404, "Invalid ID")


def _progress_rank(payload: dict) -> float:
    """How much this payload deserves to be the one shown.

    Only used to choose between the two OCR services. Both expose /progress
    but neither says which service it is; the payload shapes differ
    (8082 reports `running`/`active_page`, 8083 reports `phase`/`current_page`
    plus a one-line `message`), and neither carries a document name, so the
    counters cannot be combined. Ranking is therefore by liveness, and ties
    break toward the first URL in the list -- EXTRACTOR_URL, the scanned
    service -- because that is the one it has always been.

    A payload counts as live only if it has been touched recently. Both
    services reset the counter at the start of every request, so an idle
    service looks busy for exactly as long as its last request took to be
    noticed. Without the freshness test the rejected-PDF case is the worst
    kind of wrong: 8082 answers 422 without calling _progress_finish(), so it
    sits at `running: true` forever with zero cards, and that phantom is
    reported in preference to the 8083 run that is genuinely working.
    """
    updated = payload.get("updated_at")
    if not isinstance(updated, (int, float)):
        return -1.0
    # Nothing has touched either counter in two minutes: neither is working.
    if time.time() - updated > 120:
        return -1.0

    if payload.get("running") is True or str(payload.get("phase", "")) not in (
            "", "idle", "starting", "done"):
        return 2.0
    # Finished but fresh -- the run just ended, and its final numbers are
    # still the ones worth showing.
    return 1.0


def ocr_progress_snapshot() -> dict | None:
    """The OCR service's card counter, or None if it cannot be reached.

    Never raises. This is called while rendering a page, and an exception here
    would turn "the reader is down" into a 500 on every session page -- which
    is exactly the wrong failure: the page's real job is to report that the
    reader is down.

    A short timeout for the same reason. The counter is refreshed every few
    seconds by the browser; if OCR is wedged, holding the render open for the
    full 120s extractor timeout would make the dashboard look broken rather
    than busy. Losing the counter costs motion, not correctness -- the
    database-derived numbers are the authority and they are always present.
    """
    base_urls = [config.EXTRACTOR_URL.rsplit("/", 1)[0] + "/progress"]
    if config.OCR_ROUTER:
        text_base = config.TEXT_EXTRACTOR_URL.rsplit("/", 1)[0] + "/progress"
        if text_base not in base_urls:
            base_urls.append(text_base)

    try:
        import httpx

        # Both services are polled, and whichever is actually working is the
        # one reported. Each service keeps the counter for its own most recent
        # request only, so summing the two would mix unrelated documents -- but
        # exactly one of them is running the job at any moment, so preferring
        # the active one gives an accurate picture without cross-talk.
        best = None
        best_rank = -1.0
        for base in base_urls:
            try:
                response = httpx.get(base, timeout=2.0)
                if response.status_code != 200:
                    log.warning(f"[MAIN] OCR progress unavailable at {base}: "
                                f"HTTP {response.status_code}")
                    continue
                payload = response.json()
                if not isinstance(payload, dict):
                    continue
            except Exception as exc:  # noqa: BLE001 -- any failure means "no counter"
                log.warning(f"[MAIN] OCR progress unavailable at {base}: "
                            f"{type(exc).__name__}")
                continue

            if best is None:
                best = payload
                best_rank = _progress_rank(payload)
            elif _progress_rank(payload) > best_rank:
                best = payload
                best_rank = _progress_rank(payload)

        return best
    except Exception as exc:  # noqa: BLE001 -- any failure means "no counter"
        log.warning(f"[MAIN] OCR progress unavailable: {type(exc).__name__}")
        return None


def render(request, name, status_code=200, **context):
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)


@app.get("/health")
def health():
    log.debug(f"[MAIN] Health check requested")
    print(f"[MAIN] Health check requested")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
def home(request: Request, db: Session = Depends(get_db)):
    """The landing page.

    This route used to be a bare redirect to the upload form, which meant a
    first-time visitor landed on a file picker with no statement of what the
    system does. The counts below are the same shape the dashboard uses, so a
    fresh install with an empty database renders zeroes rather than erroring.
    """
    counts = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    total_docs = sum(counts.values())
    records = db.scalar(select(func.count()).select_from(ElectoralRecord)) or 0
    active = db.scalar(
        select(func.count()).select_from(ExtractionSession)
        .where(ExtractionSession.status == "processing")
    ) or 0
    batches = db.scalar(select(func.count()).select_from(UploadBatch)) or 0
    return render(
        request,
        "landing.html",
        counts=counts,
        total_docs=total_docs,
        records=records,
        active=active,
        batches=batches,
        done=counts.get("completed", 0),
        processing=counts.get("processing", 0),
        failed=counts.get("failed", 0),
        duplicates=counts.get("cancelled", 0),
        waiting=counts.get("pending", 0) + counts.get("retry", 0),
    )


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(admin)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    log.info(f"[MAIN] Dashboard requested")
    print(f"[MAIN] Dashboard requested")

    now_utc = datetime.now(timezone.utc)
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = now_utc.astimezone(ist)
    counts = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    total = sum(counts.values())
    completed = counts.get("completed", 0)
    processing = counts.get("processing", 0)
    waiting = sum(counts.get(status, 0) for status in ("pending", "dispatched", "queued", "paused"))
    duplicates = counts.get("cancelled", 0)
    failed = counts.get("failed", 0)
    retrying = counts.get("retry", 0)

    # "Today" and date filters follow the operator's calendar (India),
    # not UTC midnight, which lags by 5:30.
    today_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today = today_ist.astimezone(timezone.utc)
    discovered = db.scalar(select(func.count()).select_from(Document).where(Document.discovered_at >= today))
    processed = db.scalar(select(func.count()).select_from(Document).where(Document.processing_completed_at >= today))
    active = db.scalar(select(func.count()).select_from(ExtractionSession).where(ExtractionSession.status == "processing"))

    # Use only each document's current session. Historical retry attempts must
    # not inflate the page and record counters on the operations dashboard.
    session_pages, session_records = db.execute(
        select(
            func.coalesce(func.sum(ExtractionSession.pages_processed), 0),
            func.coalesce(func.sum(ExtractionSession.records_extracted), 0),
        )
        .select_from(Document)
        .join(ExtractionSession, ExtractionSession.id == Document.current_session_id)
    ).one()
    records_saved = db.scalar(select(func.count()).select_from(ElectoralRecord)) or 0

    # Timing uses the current session for each document, but keeps two clocks
    # separate: document wall time and unit elapsed time. Unit spans overlap
    # across OCR readers, so summing them is not a PDF duration.
    timing_documents = db.scalars(select(Document)).all()
    timing_units = db.scalars(
        select(ExtractionUnit)
        .join(ExtractionSession, ExtractionSession.id == ExtractionUnit.session_id)
        .join(Document, Document.current_session_id == ExtractionSession.id)
    ).all()
    total_pages = sum(
        max(0, int(u.page_to) - int(u.page_from) + 1) for u in timing_units
    )
    settled_pages = sum(
        max(0, int(u.page_to) - int(u.page_from) + 1)
        for u in timing_units if u.status == "completed"
    )
    remaining_pages = sum(
        max(0, int(u.page_to) - int(u.page_from) + 1)
        for u in timing_units if u.status in ("pending", "queued", "processing", "retry")
    )
    timing_metrics = _dashboard_timing_metrics(
        timing_documents,
        timing_units,
        total_pages=total_pages,
        completed_pages=settled_pages,
        completed_documents=completed,
        remaining_pages=remaining_pages,
    )
    avg_pdf_seconds = timing_metrics["avg_document_wall_seconds"]
    avg_seconds_per_page = timing_metrics["unit_seconds_per_page"]

    # Twelve completed-hour buckets keep the chart lightweight and keep its
    # labels aligned with the operator's IST day rather than UTC.
    current_hour_ist = now_ist.replace(minute=0, second=0, microsecond=0)
    velocity_hours = [current_hour_ist - timedelta(hours=offset) for offset in range(11, -1, -1)]
    velocity_counts = {hour: 0 for hour in velocity_hours}
    velocity_start_utc = velocity_hours[0].astimezone(timezone.utc)
    recent_completions = db.scalars(
        select(Document).where(
            Document.status == "completed",
            Document.processing_completed_at >= velocity_start_utc,
        )
    ).all()
    for doc in recent_completions:
        completed_hour = doc.processing_completed_at.astimezone(ist).replace(minute=0, second=0, microsecond=0)
        if completed_hour in velocity_counts:
            velocity_counts[completed_hour] += 1
    velocity = [
        {"label": hour.strftime("%H:%M"), "count": velocity_counts[hour]}
        for hour in velocity_hours
    ]
    velocity_max = max((bucket["count"] for bucket in velocity), default=0) or 1
    for bucket in velocity:
        bucket["height"] = max(4, round(bucket["count"] / velocity_max * 100))

    # Do not divide a brand-new run by the full twelve-hour chart width.  That
    # made, for example, 8 PDFs completed in 33 minutes look like 0.7 PDFs/h
    # merely because the preceding eleven hours had no work yet.  Use the time
    # actually observed in this rolling window, capped at twelve hours once a
    # long-running job has enough history.
    observed_start = db.scalar(
        select(func.min(Document.processing_started_at)).where(
            Document.processing_started_at.is_not(None),
            Document.processing_started_at >= velocity_start_utc,
        )
    )
    if observed_start and recent_completions:
        elapsed_hours = min(
            12.0,
            max((now_utc - observed_start).total_seconds() / 3600, 1 / 60),
        )
        throughput = len(recent_completions) / elapsed_hours
    else:
        elapsed_hours = 0.0
        throughput = 0.0
    if elapsed_hours and elapsed_hours < 12:
        observed_minutes = max(1, round(elapsed_hours * 60))
        throughput_window_label = f"observed over {observed_minutes} min"
    else:
        throughput_window_label = "rolling 12-hour window"
    remaining = processing + waiting + retrying
    page_seconds = timing_metrics["unit_seconds_per_page"]
    pages_remaining = timing_metrics["pages_remaining"]
    pages_per_hour = 3600 / page_seconds if page_seconds else 0.0
    eta_hours = pages_remaining / pages_per_hour if pages_per_hour else None
    completion_percent = round(completed / total * 100, 1) if total else 0

    stale_before = now_utc - timedelta(seconds=config.DOCUMENT_STALE_SECONDS)
    stale = db.scalar(
        select(func.count()).select_from(ExtractionUnit).where(
            ExtractionUnit.status == "processing",
            or_(ExtractionUnit.heartbeat_at.is_(None), ExtractionUnit.heartbeat_at < stale_before),
        )
    ) or 0

    source_name = func.coalesce(UploadBatch.source, Document.source_type)
    source_counts = db.execute(
        select(source_name, func.count())
        .select_from(Document)
        .outerjoin(UploadBatch, UploadBatch.id == Document.upload_batch_id)
        .group_by(source_name)
        .order_by(func.count().desc())
    ).all()
    sources = [
        {
            "name": source_type.replace("_", " ").title(),
            "count": count,
            "share": round(count / total * 100) if total else 0,
        }
        for source_type, count in source_counts
    ]

    latest_heartbeat = db.scalar(
        select(func.max(ExtractionUnit.heartbeat_at)).where(ExtractionUnit.status == "processing")
    )
    heartbeat_fresh = bool(latest_heartbeat and latest_heartbeat >= now_utc - timedelta(minutes=2))
    latest_archive = db.scalar(
        select(ArchiveIngestion).order_by(ArchiveIngestion.discovered_at.desc()).limit(1)
    )
    try:
        from redis import Redis

        redis_healthy = bool(
            Redis.from_url(
                config.REDIS_URL,
                socket_connect_timeout=0.25,
                socket_timeout=0.25,
            ).ping()
        )
    except Exception:  # The dashboard must report a broker outage, not become one.
        redis_healthy = False
    services = [
        {"name": "Web API", "status": "Healthy", "tone": "green", "detail": "Serving this dashboard"},
        {"name": "PostgreSQL", "status": "Healthy", "tone": "green", "detail": "Queries responding"},
        {
            "name": "Redis broker",
            "status": "Healthy" if redis_healthy else "Unavailable",
            "tone": "green" if redis_healthy else "red",
            "detail": "Queue broker responding" if redis_healthy else "Broker health check failed",
        },
        {
            "name": "Celery control",
            "status": "Active" if heartbeat_fresh else ("Idle" if not processing else "Delayed"),
            "tone": "blue" if heartbeat_fresh else ("gray" if not processing else "amber"),
            "detail": "Fresh worker heartbeat" if heartbeat_fresh else "No fresh document heartbeat",
        },
        {
            "name": "OCR worker",
            "status": "Active" if processing and heartbeat_fresh else ("Ready" if not processing else "Delayed"),
            "tone": "blue" if processing and heartbeat_fresh else ("green" if not processing else "amber"),
            "detail": f"{processing} document{'s' if processing != 1 else ''} in progress",
        },
        {
            "name": "Paddle reader",
            "status": "Active" if processing and heartbeat_fresh else "Ready",
            "tone": "blue" if processing and heartbeat_fresh else "green",
            "detail": "Protected sequential OCR lane",
        },
        {
            "name": "MinIO storage",
            "status": "Healthy" if not latest_archive or latest_archive.status != "failed" else "Needs attention",
            "tone": "green" if not latest_archive or latest_archive.status != "failed" else "red",
            "detail": "Latest archive available" if latest_archive else "No archive received yet",
        },
    ]

    pipeline_state = "Healthy"
    pipeline_tone = "green"
    if failed or stale:
        pipeline_state, pipeline_tone = "Needs attention", "red"
    elif retrying:
        pipeline_state, pipeline_tone = "Recovering", "amber"
    elif not processing and waiting:
        pipeline_state, pipeline_tone = "Waiting", "amber"

    log.info(f"[MAIN] Dashboard data: counts={counts}, discovered={discovered}, processed={processed}, active={active}")
    print(f"[MAIN] Dashboard data: counts={counts}, discovered={discovered}, processed={processed}, active={active}")

    return render(
        request,
        "dashboard.html",
        counts=counts,
        total=total,
        completed=completed,
        processing=processing,
        waiting=waiting,
        duplicates=duplicates,
        failed=failed,
        retrying=retrying,
        stale=stale,
        discovered=discovered or 0,
        processed=processed or 0,
        active=active or 0,
        pages_processed=int(session_pages or 0),
        records_extracted=int(session_records or 0),
        records_saved=int(records_saved),
        avg_pdf_seconds=avg_pdf_seconds,
        avg_seconds_per_page=avg_seconds_per_page,
        unit_elapsed_seconds=timing_metrics["unit_elapsed_seconds"],
        unit_pages_measured=timing_metrics["unit_pages_measured"],
        pages_remaining=pages_remaining,
        pages_per_hour=pages_per_hour,
        throughput=throughput,
        throughput_window_label=throughput_window_label,
        eta_hours=eta_hours,
        remaining=remaining,
        completion_percent=completion_percent,
        velocity=velocity,
        sources=sources,
        services=services,
        pipeline_state=pipeline_state,
        pipeline_tone=pipeline_tone,
        updated_at=now_ist,
    )


@app.get("/upload", response_class=HTMLResponse, dependencies=[Depends(admin)])
def upload_page(request: Request, uploaded: int = 0, batch: str = "", notice: str = "", db: Session = Depends(get_db)):
    # No cap. The page used to keep the 20 newest rows, which dropped any
    # older ZIP that was still in the bucket — including one whose extract
    # had already finished — while the heading still said the list was complete.
    archives = db.scalars(select(ArchiveIngestion).order_by(ArchiveIngestion.discovered_at.desc())).all()
    uploads = _uploads_tree(db)
    return render(
        request,
        "uploads.html",
        uploaded=uploaded,
        batch=batch,
        # The page itself only summarises the uploads; each one's full batch
        # list now lives on its own /uploads/<id> page. These entries carry
        # the counts the summary cards need and the id the links target.
        uploads=uploads,
        batch_size=config.UPLOAD_BATCH_SIZE,
        minio_enabled=config.MINIO_ENABLED,
        minio_console_url=MINIO_SSO_PATH,
        minio_bucket=config.MINIO_BUCKET,
        # Shown on the ZIP mode card, next to the console link. The person
        # asked to drop the archive in is the operator standing at this app,
        # not necessarily whoever set MinIO up, and sending them hunting for
        # credentials is the step where a ZIP upload stops happening at all.
        minio_access_key=config.MINIO_ACCESS_KEY,
        minio_secret_key=config.MINIO_SECRET_KEY,
        minio_prefix=f"{config.MINIO_PREFIX}/" if config.MINIO_PREFIX else "", 
        archives=archives,
        archive_uploads={a.id: _upload_for_archive(db, a) for a in archives},
        notice=notice,
        error=None,
    )


# Browser-side multipart upload of ZIPs straight into the MinIO bucket. The
# public hostname is behind Cloudflare, which rejects any single request body
# over 100 MB, so a 5-10 GB archive is sent as numbered parts below that size
# and MinIO stitches them back together. Each part is held in memory once.
ZIP_PART_SIZE = 50 * 1024 * 1024
ZIP_PART_MAX = 64 * 1024 * 1024


def _zip_object_key(filename: str) -> str:
    stem = Path(filename or "").stem
    prefix = f"{config.MINIO_PREFIX}/" if config.MINIO_PREFIX else ""
    return f"{prefix}{safe_batch_name(stem)}.zip"


def _check_zip_key(key: str) -> None:
    prefix = f"{config.MINIO_PREFIX}/" if config.MINIO_PREFIX else ""
    if not key.startswith(prefix) or not key.lower().endswith(".zip") or ".." in key:
        raise HTTPException(status_code=400, detail="Invalid upload key.")


@app.post("/upload/zip/start", dependencies=[Depends(admin)])
async def zip_upload_start(request: Request):
    if not config.MINIO_ENABLED:
        raise HTTPException(status_code=400, detail="MinIO is not enabled.")
    payload = await request.json()
    filename = str(payload.get("filename") or "")
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files can be uploaded here.")
    key = _zip_object_key(filename)

    def start():
        client = s3_client()
        ensure_bucket(client)
        return client.create_multipart_upload(
            Bucket=config.MINIO_BUCKET, Key=key, ContentType="application/zip"
        )["UploadId"]

    upload_id = await asyncio.to_thread(start)
    return {"upload_id": upload_id, "key": key, "part_size": ZIP_PART_SIZE}


@app.put("/upload/zip/part", dependencies=[Depends(admin)])
async def zip_upload_part(request: Request, upload_id: str, key: str, part_number: int):
    _check_zip_key(key)
    if not 1 <= part_number <= 10000:
        raise HTTPException(status_code=400, detail="Invalid part number.")
    body = await request.body()
    if not body or len(body) > ZIP_PART_MAX:
        raise HTTPException(status_code=400, detail="Invalid part size.")

    def put():
        return s3_client().upload_part(
            Bucket=config.MINIO_BUCKET, Key=key, UploadId=upload_id,
            PartNumber=part_number, Body=body,
        )["ETag"]

    etag = await asyncio.to_thread(put)
    return {"etag": etag}


@app.post("/upload/zip/complete", dependencies=[Depends(admin)])
async def zip_upload_complete(request: Request):
    payload = await request.json()
    key, upload_id = str(payload.get("key") or ""), str(payload.get("upload_id") or "")
    _check_zip_key(key)
    parts = sorted(
        ({"PartNumber": int(p["part_number"]), "ETag": str(p["etag"])} for p in payload.get("parts") or []),
        key=lambda p: p["PartNumber"],
    )
    if not parts:
        raise HTTPException(status_code=400, detail="No parts were uploaded.")

    def complete():
        s3_client().complete_multipart_upload(
            Bucket=config.MINIO_BUCKET, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    await asyncio.to_thread(complete)
    return {"key": key, "bucket": config.MINIO_BUCKET}


@app.post("/upload/zip/abort", dependencies=[Depends(admin)])
async def zip_upload_abort(request: Request):
    payload = await request.json()
    key, upload_id = str(payload.get("key") or ""), str(payload.get("upload_id") or "")
    _check_zip_key(key)
    await asyncio.to_thread(
        lambda: s3_client().abort_multipart_upload(Bucket=config.MINIO_BUCKET, Key=key, UploadId=upload_id)
    )
    return {"aborted": True}


def _upload_for_archive(db: Session, archive: ArchiveIngestion) -> UploadBatch | None:
    """The UploadBatch a MinIO archive produced, or None.

    Nothing links these two rows: archive_ingest.py creates the batch with the
    same `directory_name` it records as ArchiveIngestion.output_directory, so
    the name is the join. Matching on it here is what lets "Recent ZIP uploads"
    link through to the batch's own page. Older archives and failed ingestions
    have no batch, and return None so the row renders unlinked rather than
    pointing at a dead /uploads/None.
    """
    if not archive.output_directory:
        return None
    return db.scalar(
        select(UploadBatch)
        .where(UploadBatch.relative_directory == archive.output_directory)
        .order_by(UploadBatch.created_at.desc())
    )


@app.get("/uploads", dependencies=[Depends(admin)])
def uploads_redirect():
    """The old page path.

    The upload page moved to /upload and its URL is the thing people bookmark,
    so this keeps that link alive. It is also what makes the /uploads/<id>
    detail routes below unambiguous: without it, GET /uploads would be matched
    against the detail route with an empty id.
    """
    return RedirectResponse("/upload", status_code=303)


# The four buckets the per-upload progress bar divides a batch into. A
# document is in exactly one of them and they run left to right in the order
# the work does: waiting to start, in motion, finished, needs attention.
#
# Anything not explicitly recognised falls into "waiting" rather than being
# dropped, so a status added later still shows up in the total rather than
# making the bar disagree with the count beside it.
PROGRESS_BUCKETS = {
    "done": ("completed",),
    "active": ("queued", "processing", "retry"),
    # Duplicate-content cancellations are selected by error code in
    # _progress_of(). They are not failures and must never inflate the red
    # "Needs attention" count.
    "skipped": (),
    "problem": ("failed", "cancelled", "partial"),
    "waiting": (),  # the fallback: anything that has not started
}


def _progress_of(docs: list) -> tuple[dict, dict]:
    """(counts, percentages) per status bucket for one upload.

    The page answers one question -- "did my 300 rolls go through?" -- and a
    bare total cannot answer it. Counting here rather than in Jinja keeps the
    arithmetic out of the template and lets the bar and the sentence beside it
    read from the same numbers, so they cannot drift apart.
    """
    lookup = {name: set(values) for name, values in PROGRESS_BUCKETS.items()}
    counts = {name: 0 for name in lookup}
    for doc in docs:
        status = str(doc.status or "").lower()
        if status == "cancelled" and getattr(doc, "last_error_code", None) == "DUPLICATE_CONTENT":
            counts["skipped"] += 1
            continue
        for name, values in lookup.items():
            if status in values:
                counts[name] += 1
                break
        else:
            counts["waiting"] += 1

    total = len(docs)
    # Whole percent, not two decimals. 81.27% of 582 PDFs implies a precision
    # the number does not have -- nobody acts on the difference between 81% and
    # 82% -- and the trailing digits made the five cards read as a wall of
    # figures rather than five states. The exact count sits beside each one and
    # that is the number to act on.
    pct = {
        name: (round(100.0 * value / total) if total else 0)
        for name, value in counts.items()
    }
    return counts, pct


def _uploads_tree(db: Session) -> list:
    """The whole /uploads hierarchy: uploads -> batches of N -> documents.

    Chunking is done here rather than in the template. Doing it in Jinja would
    mean arithmetic and slicing inside a nested loop, which is exactly the kind
    of thing that is unreadable to fix later -- and the batch boundaries have
    to agree with the counts shown in the headings, so there is one place that
    decides where a batch starts and ends.

    The document count shown is the number that actually registered, not the
    number the upload claimed. They differ when a duplicate PDF is rejected by
    uq_documents_hash, and "300 uploaded, 298 arrived" is the fact the user
    needs in order to notice something went wrong.
    """
    uploads = db.scalars(
        select(UploadBatch).order_by(UploadBatch.created_at.desc()).limit(50)
    ).all()
    grouped: dict = {}
    if uploads:
        rows = db.execute(
            select(Document).where(
                Document.upload_batch_id.in_([u.id for u in uploads])
            ).order_by(Document.batch_index)
        ).scalars().all()
        for doc in rows:
            grouped.setdefault(doc.upload_batch_id, []).append(doc)

    return [_upload_entry(upload, grouped.get(upload.id, [])) for upload in uploads]


def _documents_page_groups(
    rows: list,
    *,
    chunk_size: int,
    batch_registered: dict | None = None,
    open_batch: str = "",
) -> list[dict]:
    """Shape the /documents list: upload batch headers, then fixed-size sub-batches.

    Pagination slices through documents, so a page may contain only part of an
    upload. Headers still name the owning batch and link to its upload page;
    sub-batch strips reuse the same numbering as /uploads/{id}.

    Each sub-batch carries its own counts and percentages, not just a slice of
    documents. The strip is where the reader is anyway -- they are looking at
    the PDFs under that heading -- and a progress readout sitting on the frame
    above spoke about all 582 at once, which is a number you act on once a day
    rather than a number that belongs to the 100 rows you are reading. Counting
    here, over the same chunk the rows below come from, is what keeps the two
    from disagreeing.

    `open_batch` names the one sub-batch whose documents are carried through to
    the template as rows: "<upload id>-<batch number>". Every other chunk
    still carries its full documents here, because that is what its counts are
    computed from -- the strips have to stay accurate whether or not anyone
    opened them. The caller drops the closed chunks' rows before rendering, so
    the saving is in the response rather than in this function.
    """
    if not rows:
        return []

    registered = batch_registered or {}
    groups: list[dict] = []
    bucket: list = []
    bucket_upload: UploadBatch | None = None
    bucket_id = None

    def flush() -> None:
        nonlocal bucket, bucket_upload, bucket_id
        if not bucket:
            return
        subgroups = []
        for i in range(0, len(bucket), chunk_size):
            chunk = bucket[i : i + chunk_size]
            bi_start = chunk[0].batch_index
            counts, pct = _progress_of(chunk)
            settled = counts["done"] + counts["active"] + counts["skipped"] + counts["problem"]
            batch_num = ((bi_start - 1) // chunk_size + 1) if bi_start else (len(subgroups) + 1)
            subgroups.append(
                {
                    "batch_num": batch_num,
                    # The template needs the same key the strip's View button is
                    # built from, so it is computed once and both read it.
                    "key": f"{bucket_id or 'other'}-{batch_num}",
                    "size": len(chunk),
                    "settled": settled,
                    "counts": counts,
                    "pct": pct,
                    "documents": chunk,
                }
            )
        total_in_db = registered.get(bucket_id, len(bucket)) if bucket_id else len(bucket)
        on_page = len(bucket)
        counts, pct = _progress_of(bucket)
        groups.append(
            {
                "upload": bucket_upload,
                "registered": total_in_db,
                "on_page": on_page,
                # Only the on-page rows are counted, never every document the
                # upload registered. In the all-uploads view a frame is 50 rows
                # out of 582, so an upload-wide total sat directly above a list
                # it could not describe -- "476 finished" above 50 rows, seven
                # of them finished.
                "counts": counts,
                "pct": pct,
                "subgroups": subgroups,
            }
        )
        bucket = []

    for doc in rows:
        if bucket and doc.upload_batch_id != bucket_id:
            flush()
        bucket_id = doc.upload_batch_id
        bucket_upload = doc.upload_batch
        bucket.append(doc)
    flush()
    return groups


def _upload_entry(upload: UploadBatch, docs: list) -> dict:
    """One upload's row in the tree: its documents, their batches, its counts.

    Factored out of _uploads_tree so the per-upload page can build a single
    entry with the identical shape -- the progress header and the batch strips
    read the same keys, so the two pages cannot drift apart.
    """
    size = config.UPLOAD_BATCH_SIZE
    counts, pct = _progress_of(docs)
    return {
        "upload": upload,
        "documents": docs,
        "groups": [docs[i:i + size] for i in range(0, len(docs), size)],
        "registered": len(docs),
        "missing": max(0, upload.file_count - len(docs)),
        "counts": counts,
        "pct": pct,
    }


# Where a bulk action lands when the page that issued it cannot be named.
# /documents rather than /upload: the upload form is where a run STARTS, and
# the thing someone wants after saying "extract these" is the list those PDFs
# are in and whether they are moving. Sending them back to a file picker threw
# away the only page that answers both.
_BULK_ACTION_FALLBACK = "/documents"

# The pages a bulk action is allowed to bounce back to. Anything else is
# ignored in favour of the fallback -- return_to comes straight off a hidden
# form field, so honouring it verbatim would be an open redirect on this host
# even though it only ever points at itself. The per-upload pages are
# /uploads/{uuid}, hence the prefix rather than a third exact match.
_BULK_ACTION_RETURN_PREFIXES = ("/documents", "/upload", "/uploads")


def _bulk_action_redirect(return_to: str | None, message: str, *, code: int = 303) -> RedirectResponse:
    """After bulk extract/delete/export-options, land on the page that issued the action."""
    path = (return_to or "").split("?", 1)[0]
    allowed = any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _BULK_ACTION_RETURN_PREFIXES
    )
    if allowed:
        sep = "&" if "?" in return_to else "?"
        target = f"{return_to}{sep}{urlencode({'notice': message})}"
    else:
        target = f"{_BULK_ACTION_FALLBACK}?{urlencode({'notice': message})}"
    return RedirectResponse(target, status_code=code)


def _page_for_documents(db: Session, document_ids: list[str], notice: str, code: int = 303):
    """Send a row action back to the page the row was clicked on.

    Rows live on the upload page and on each upload's own page, and both call
    the same /uploads/document/... endpoints. Redirecting everything to /upload
    would throw a user who acted on batch 4 of a 582-PDF upload back to the top
    of the upload list, so the owning batch decides the target. The page is the
    one the action was issued from, which is derivable from the documents in it.
    """
    batch_id = None
    for did in document_ids:
        try:
            doc = db.get(Document, parse_uuid(did))
        except HTTPException:
            continue
        batch_id = doc.upload_batch_id if doc else None
        break

    query = urlencode({"notice": notice})
    # The row actions on a batch's own page used to bounce back to
    # /uploads/{id}, which rendered a page that no longer exists. They land on
    # /documents?upload=<id> now -- the same view, under its current name.
    target = f"/documents?upload={batch_id}&{query}" if batch_id else f"/upload?{query}"
    return RedirectResponse(target, status_code=code)


def _uploads_context(db: Session, *, uploaded: int = 0, batch: str = "", error=None) -> dict:
    """Shared context for the re-rendered error paths.

    A failed upload re-renders the page rather than redirecting, so it has to
    rebuild the same context upload_page builds. Duplicating that query inline
    in each error branch is how the two versions drift apart.
    """
    archives = db.scalars(select(ArchiveIngestion).order_by(ArchiveIngestion.discovered_at.desc())).all()
    return dict(
        uploaded=uploaded,
        batch=batch,
        # The error paths re-render uploads.html, which lists the uploads as
        # summary rows now -- the full batch strips live on each upload's own
        # page, so the tree is only needed for its counts and ids here.
        uploads=_uploads_tree(db),
        archive_uploads={a.id: _upload_for_archive(db, a) for a in archives},
        batch_size=config.UPLOAD_BATCH_SIZE,
        minio_enabled=config.MINIO_ENABLED,
        minio_console_url=MINIO_SSO_PATH,
        minio_bucket=config.MINIO_BUCKET,
        # Shown on the ZIP mode card, next to the console link. The person
        # asked to drop the archive in is the operator standing at this app,
        # not necessarily whoever set MinIO up, and sending them hunting for
        # credentials is the step where a ZIP upload stops happening at all.
        minio_access_key=config.MINIO_ACCESS_KEY,
        minio_secret_key=config.MINIO_SECRET_KEY,
        minio_prefix=f"{config.MINIO_PREFIX}/" if config.MINIO_PREFIX else "", 
        archives=archives,
        notice="",
        error=error,
    )


@app.post("/upload", response_class=HTMLResponse, dependencies=[Depends(admin)])
async def upload_pdfs(
    request: Request,
    files: list[UploadFile] = File(...),
    batch_label: str = Form("upload"),
    upload_source: str = Form("files"),
):
    try:
        summary = await save_upload_batch(files, batch_label)
    except UploadValidationError as exc:
        with SessionLocal() as db:
            return render(request, "uploads.html", status_code=400,
                          **_uploads_context(db, error=str(exc)))
    except OSError:
        log.exception("Could not save uploaded PDFs to %s", config.DOCUMENT_ROOT)
        with SessionLocal() as db:
            return render(request, "uploads.html", status_code=500, **_uploads_context(
                db, error="The server could not save this upload. Check the upload volume "
                          "and available disk space."))

    # The batch is the row the page hangs everything off. Created before
    # discovery so the documents it registers already have somewhere to point.
    #
    # The label is stored exactly as typed -- NOT the sanitised directory name.
    # safe_batch_name() exists to make a safe path component and turns
    # "Delhi North / Ward 12" into "Delhi-North-Ward-12"; that mangling is
    # right for the directory and wrong for a name someone has to read and
    # edit later. An empty name falls back to the sanitised form so the column
    # is never blank.
    with SessionLocal.begin() as db:
        upload_batch = UploadBatch(
            label=(batch_label or "").strip()[:120] or summary.batch_name,
            source=upload_source if upload_source in ("files", "folder") else "files",
            relative_directory=summary.relative_directory,
            file_count=summary.file_count,
        )
        db.add(upload_batch)
        db.flush()
        batch_id = upload_batch.id

    # Discovered inline rather than by publishing a task. The previous code
    # called discover_documents.delay() here, which found the files but left
    # them approved=True and running within a beat tick -- the user had no
    # chance to see a wrong file first. discover_upload_batch passes
    # approved=False, so nothing starts until Extract is pressed.
    try:
        discover_upload_batch(batch_id)
    except Exception:
        log.exception("Upload saved, but its PDFs could not be registered")

    # Straight to the new batch's own page rather than back to the upload page.
    # The upload page no longer lists every batch -- it explains the three ways
    # in and then hands off -- so bouncing there would show no sign that the
    # files just uploaded, and the user would have to go and find their upload
    # in a list. The detail page is where the file list, the counts and the
    # Start extraction button already are.
    query = urlencode({"notice": f"{summary.file_count} PDF file(s) uploaded. "
                                 "Nothing runs until you press Start extraction."})
    return RedirectResponse(f"/uploads/{batch_id}?{query}", status_code=303)


# The columns /uploads/export can emit. Built once so the route and the
# upcoming column picker cannot drift apart: the same key serves both the
# CSV header and the model attribute it reads from.
_EXPORT_COLUMNS = [
    ("attempt", "attempt"),
    ("sno", "sno"),
    ("page_number", "page_number"),
    ("card_index", "card_index"),
    ("voter_sr_no", "voter_sr_no"),
    ("id_card_no", "epic_number"),
    ("gender", "gender_hi"),
    ("age", "age"),
    ("house_no", "house_number_hi"),
    ("voter_first_name", "voter_first_name_hi"),
    ("voter_middle_name", "voter_middle_name_hi"),
    ("voter_sur_name", "voter_sur_name_hi"),
    ("relation_name", "relation_name"),
    ("voter_father_first_name", "voter_father_first_name_hi"),
    ("voter_father_middle_name", "voter_father_middle_name_hi"),
    ("voter_father_last_name", "voter_father_last_name_hi"),
    ("voter_husband_first_name", "voter_husband_first_name_hi"),
    ("voter_husband_middle_name", "voter_husband_middle_name_hi"),
    ("voter_husband_last_name", "voter_husband_last_name_hi"),
    ("voter_mother_first_name", "voter_mother_first_name_hi"),
    ("voter_mother_middle_name", "voter_mother_middle_name_hi"),
    ("voter_mother_last_name", "voter_mother_last_name_hi"),
    ("voter_other_first_name", "voter_other_first_name_hi"),
    ("voter_other_middle_name", "voter_other_middle_name_hi"),
    ("voter_other_last_name", "voter_other_last_name_hi"),
    ("is_deleted", "is_deleted"),
    ("state_code", "state_code"),
    ("ac_code", "ac_code"),
    ("anubhag_code", "anubhag_code"),
    ("anubhag_name", "anubhag_name"),
    ("booth_code", "booth_code"),
    ("pdf_name", "pdf_name"),
    ("needs_review", "needs_review"),
    ("review_reasons", "review_reasons"),
    ("field_sources", "field_sources"),
    ("serial_number", "serial_number"),
    ("epic_number", "epic_number"),
    ("name_hi", "name_hi"),
    ("name_en", "name_en"),
    ("relative_name_hi", "relative_name_hi"),
    ("relative_name_en", "relative_name_en"),
    ("relationship_type", "relationship_type"),
    ("gender_hi", "gender_hi"),
    ("gender_en", "gender_en"),
    ("house_number_hi", "house_number_hi"),
    ("house_number_en", "house_number_en"),
    ("section_number", "section_number"),
    ("section_name_hi", "section_name_hi"),
    ("section_name_en", "section_name_en"),
    ("confidence", "confidence"),
    ("source_record_hash", "source_record_hash"),
    ("translit_source_hash", "translit_source_hash"),
    ("is_valid", "is_valid"),
    ("validation_errors", "validation_errors"),
]


def _build_document_export_xlsx(document_ids: list[str], chosen: list[str]) -> str:
    """Build a selected-document workbook away from the async web loop.

    Large exports can contain millions of cells. This function owns its own
    database session because it runs in a worker thread; sharing the request
    session across threads would be unsafe. The caller only returns the file
    after this is complete, while the web event loop remains available for the
    dashboard and document pages.
    """
    import json
    import tempfile
    from openpyxl import Workbook

    attr_map = dict(_EXPORT_COLUMNS)
    with SessionLocal() as export_db:
        docs_by_id = {}
        for did in document_ids:
            try:
                doc = export_db.get(Document, parse_uuid(did))
            except HTTPException:
                doc = None
            if doc is not None:
                docs_by_id[str(doc.id)] = doc
        if not docs_by_id:
            raise ValueError("No valid PDFs were selected")

        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Voter records")
        sheet.freeze_panes = "A2"
        sheet.append(chosen)
        review_rows = []
        for doc in docs_by_id.values():
            records, record_session = records_for_document(export_db, doc.id)
            attempt = record_session.attempt_number if record_session else ""
            review_rows.extend(completeness_rows(records))
            for record in records:
                values = []
                for label in chosen:
                    attr = attr_map[label]
                    value = attempt if label == "attempt" else getattr(record, attr, "")
                    if attr in {"house_number_hi", "house_number_en"}:
                        value = house_number_display(value)
                    if label in ("review_reasons", "field_sources", "validation_errors"):
                        value = json.dumps(value, ensure_ascii=False) if value else ""
                    values.append(value if value is not None else "")
                sheet.append(values)

        review = workbook.create_sheet("Review summary")
        review.freeze_panes = "A2"
        review.append([
            "document_id", "page_number", "card_index", "missing_fields",
            "review_reasons", "is_valid",
        ])
        for row in review_rows:
            review.append([
                row["document_id"], row["page_number"], row["card_index"],
                row["missing_fields"], row["review_reasons"], row["is_valid"],
            ])

        temporary = tempfile.NamedTemporaryFile(
            prefix="electoral-records-", suffix=".xlsx", delete=False
        )
        temporary_path = temporary.name
        temporary.close()
        try:
            workbook.save(temporary_path)
        except Exception:
            Path(temporary_path).unlink(missing_ok=True)
            raise
    return temporary_path


@app.get("/uploads/export-options", response_class=HTMLResponse, dependencies=[Depends(admin)])
def upload_export_options_empty(request: Request):
    """Give direct/bookmarked visits a useful page instead of a 405 error."""
    return render(
        request,
        "export_options.html",
        document_ids=[],
        columns=_EXPORT_COLUMNS,
        lang="both",
        return_to="/documents",
    )


@app.post("/uploads/export-options", dependencies=[Depends(admin)])
async def upload_export_options(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    document_ids = form.getlist("document_ids")
    return_to = form.get("return_to")
    if not document_ids:
        return _bulk_action_redirect(
            str(return_to) if return_to else None,
            "Tick at least one PDF to export.",
        )
    valid_ids = []
    for raw_id in dict.fromkeys(document_ids):
        try:
            document_uuid = parse_uuid(str(raw_id))
        except HTTPException:
            continue
        if db.get(Document, document_uuid) is not None:
            valid_ids.append(str(document_uuid))
    if not valid_ids:
        return _bulk_action_redirect(
            str(return_to) if return_to else None,
            "None of the selected PDFs could be found.",
        )

    return render(
        request,
        "export_options.html",
        document_ids=valid_ids,
        columns=_EXPORT_COLUMNS,
        lang="both",
        return_to=str(return_to) if return_to else "",
    )


@app.post("/uploads/export", dependencies=[Depends(admin)])
async def upload_export(request: Request, db: Session = Depends(get_db)):
    """Export all selected PDFs into one worksheet in one Excel workbook.

    The existing latest-attempt record scoping remains unchanged. A write-only
    workbook avoids retaining hundreds of thousands of cell objects in web
    memory when a complete upload is selected.
    """
    form = await request.form()
    document_ids = form.getlist("document_ids")
    chosen = form.getlist("columns")
    lang = str(form.get("lang") or "both")
    if not chosen:
        raise HTTPException(400, "Choose at least one export column")
    known = {label for label, _ in _EXPORT_COLUMNS}
    chosen = [c for c in chosen if c in known]
    if not chosen:
        raise HTTPException(400, "Choose at least one valid export column")

    # Apply language filter
    attr_map = dict(_EXPORT_COLUMNS)
    if lang not in {"both", "hi", "en"}:
        lang = "both"
    if lang == "hi":
        chosen = [c for c in chosen if not attr_map.get(c, "").endswith("_en")]
    elif lang == "en":
        chosen = [c for c in chosen if not attr_map.get(c, "").endswith("_hi")]
    if not chosen:
        raise HTTPException(400, "The selected language leaves no export columns")

    valid_document_ids = []
    for did in document_ids:
        try:
            doc = db.get(Document, parse_uuid(did))
        except HTTPException:
            doc = None
        if doc is not None:
            valid_document_ids.append(str(doc.id))
    if not valid_document_ids:
        raise HTTPException(400, "No valid PDFs were selected")

    try:
        temporary_path = await asyncio.to_thread(
            _build_document_export_xlsx, valid_document_ids, chosen
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return FileResponse(
        temporary_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="electoral_records.xlsx",
        background=BackgroundTask(Path(temporary_path).unlink, missing_ok=True),
    )


@app.post("/uploads/extract", dependencies=[Depends(admin)])
def upload_extract(
    request: Request,
    document_ids: list[str] = Form(default=[]),
    return_to: str = Form(default=""),
):
    """Approve the ticked PDFs. The beat task picks them up within ~15s."""
    accepted, skipped = select_documents_for_extraction(document_ids)
    back = return_to or None
    if not document_ids:
        return _bulk_action_redirect(back, "Tick at least one PDF first.", code=400)
    if accepted:
        message = f"Started extraction for {accepted} PDF(s)."
        if skipped:
            message += f" {skipped} skipped because they are already finished, skipped, or running."
        return _bulk_action_redirect(back, message)
    return _bulk_action_redirect(
        back,
        "Nothing was queued. The selected PDFs are already finished, skipped, or running.",
        code=409,
    )


@app.post("/uploads/delete", dependencies=[Depends(admin)])
def upload_delete(
    request: Request,
    document_ids: list[str] = Form(default=[]),
    return_to: str = Form(default=""),
):
    back = return_to or None
    if not document_ids:
        return _bulk_action_redirect(back, "Tick at least one PDF to delete.", code=400)
    deleted, skipped = delete_documents(document_ids)
    message = f"Deleted {deleted} PDF(s) and everything extracted from them."
    if skipped:
        message += f" {skipped} skipped because they are still running."
    return _bulk_action_redirect(back, message)


@app.get("/documents", response_class=HTMLResponse, dependencies=[Depends(admin)])
def documents(
    request: Request,
    status: str = "",
    filename: str = "",
    date_from: str = "",
    session_id: str = "",
    document_id: str = "",
    upload: str = "",
    unassigned: bool = False,
    open_batch: str = "",
    page: int = 1,
    notice: str = "",
    db: Session = Depends(get_db),
):
    """The document list, in two scopes.

    One route answers both questions that used to need two pages. With no
    `upload` it lists every upload, searchable and filterable, which is what
    /documents has always been. With `upload=<uuid>` it narrows to that one
    upload and shows its full PDF list -- which is exactly what
    /uploads/{id} showed, so that route now redirects here rather than
    rendering a second template for the same view.

    Keeping both in one handler is the point: the filters, the progress
    readout, the batch strips and the selection bar were ~95% identical in
    the two templates, and identical markup in two files is how they drift.

    `open_batch` is the one piece of state that is not a filter. It names the
    single sub-batch whose PDFs are actually listed -- "<upload id>-<n>" -- and
    the default is the empty string, which means every strip shows its numbers
    and none of them shows filenames. That is a real change in what the page
    sends, not a hidden attribute: six batches' worth of rows used to be in
    every response whether or not anyone could see them.
    """
    log.info(f"[MAIN] Documents page requested with filters: status={status}, filename={filename}, date_from={date_from}, session_id={session_id}, document_id={document_id}, upload={upload}, page={page}")
    print(f"[MAIN] Documents page requested with filters: status={status}, filename={filename}, date_from={date_from}, session_id={session_id}, document_id={document_id}, upload={upload}, page={page}")

    page = max(page, 1)
    all_total = db.scalar(select(func.count()).select_from(select(Document).subquery()))

    query = select(Document).outerjoin(UploadBatch, UploadBatch.id == Document.upload_batch_id)
    if status:
        query = query.where(Document.status == status)
    if filename:
        query = query.where(or_(Document.file_name.ilike(f"%{filename}%"), Document.source_path.ilike(f"%{filename}%")))
    if date_from:
        try:
            day_ist = datetime.combine(date.fromisoformat(date_from), datetime.min.time(), tzinfo=timezone(timedelta(hours=5, minutes=30)))
            query = query.where(Document.discovered_at >= day_ist.astimezone(timezone.utc))
        except ValueError:
            log.error(f"[MAIN] Invalid date_from format: {date_from}")
            print(f"[MAIN] Invalid date_from format: {date_from}")
            raise HTTPException(400, "Invalid date_from")
    if session_id:
        query = query.where(Document.current_session_id == parse_uuid(session_id))
    if document_id:
        query = query.where(Document.id == parse_uuid(document_id))

    # A bad uuid in the query string is a stale bookmark, not an attack. 404
    # would be the wrong shape for it: the person gets a dead page and no way
    # back. Falling through to the unfiltered list shows them the app still
    # works and lets them pick an upload from there.
    focus = None
    if upload:
        try:
            focus = db.get(UploadBatch, parse_uuid(upload))
        except HTTPException:
            focus = None
        if focus is None:
            log.warning(f"[MAIN] Unknown upload in /documents?upload=..., showing all: {upload}")
        else:
            query = query.where(Document.upload_batch_id == focus.id)
    elif unassigned:
        query = query.where(Document.upload_batch_id.is_(None))

    # How much this view shows at once: all of it.
    #
    # The all-uploads list used to be cut to 50 rows across 12 pages, while the
    # scoped view listed every PDF in the upload. Same page, same design, two
    # different lists -- so "Documents" showed one batch strip and a Next
    # button, and the page you land on first was the poorer of the two.
    #
    # One rule for both scopes now. Every PDF that matches the filter is in
    # the DOM, and find-in-page covers it. A server-side cut was defending
    # against a page too tall to scroll, which collapse-by-batch already
    # handles, and it cost the one thing people open this page for: the
    # ability to see, at a glance, what is finished and what is still stuck.
    #
    # 600 rows is a large page and that is the trade being made knowingly. The
    # strips fold, so collapsed it is roughly a screen. Nothing is withheld;
    # the only cost is the DOM.
    page_size = None

    total = db.scalar(select(func.count()).select_from(query.subquery()))

    # The summary counts the same set the list shows, so the five cards and the
    # rows can never contradict each other. Scoped to one upload this is that
    # upload's breakdown; in the all-uploads view it is the whole system's,
    # which is what it always was. Counting every status in the table instead
    # is what made the cards read 603 "Finished" above a list of one upload.
    summary_counts = dict(
        db.execute(
            query.with_only_columns(Document.status, func.count())
            .group_by(Document.status)
        ).all()
    )
    summary = {
        "completed": summary_counts.get("completed", 0),
        "processing": summary_counts.get("processing", 0),
        "waiting": sum(summary_counts.get(value, 0) for value in ("pending", "dispatched", "queued", "paused")),
        "attention": summary_counts.get("failed", 0) + summary_counts.get("retry", 0),
        "duplicates": summary_counts.get("cancelled", 0),
    }

    listing = query.options(selectinload(Document.upload_batch)).order_by(
        nulls_last(UploadBatch.created_at.desc()),
        Document.batch_index.asc(),
        Document.discovered_at.desc(),
    )
    if page_size:
        listing = listing.limit(page_size).offset((page - 1) * page_size)

    rows = db.scalars(listing).all()
    upload_ids = {d.upload_batch_id for d in rows if d.upload_batch_id}
    batch_registered = (
        dict(
            db.execute(
                select(Document.upload_batch_id, func.count())
                .where(Document.upload_batch_id.in_(upload_ids))
                .group_by(Document.upload_batch_id)
            ).all()
        )
        if upload_ids
        else {}
    )
    batch_groups = _documents_page_groups(
        list(rows),
        chunk_size=config.UPLOAD_BATCH_SIZE,
        batch_registered=batch_registered,
    )

    # Keep selection IDs even when the visual filename rows are removed below.
    # Previously that optimisation left the visible batch checkboxes with
    # nothing to select or submit.
    for group in batch_groups:
        for subgroup in group["subgroups"]:
            subgroup["selection_ids"] = [str(doc.id) for doc in subgroup["documents"]]

    # Drop the rows of every batch that is not the open one.
    #
    # The strips above are computed from the same documents, so they stay
    # correct after this -- a closed strip still knows its own five counts
    # because those were counted before anything was thrown away. All that goes
    # is the filename list the reader cannot see. On a 582-PDF roll this is the
    # difference between a page that lists six batches and one that lists 582
    # filenames behind six summaries.
    open_key = open_batch.strip()
    if open_key:
        for group in batch_groups:
            for subgroup in group["subgroups"]:
                if subgroup["key"] != open_key:
                    subgroup["documents"] = []
    else:
        for group in batch_groups:
            for subgroup in group["subgroups"]:
                subgroup["documents"] = []
    # No per-upload query loop here any more. It existed to hand each frame the
    # totals of its whole upload -- 582 documents, one query per upload -- and
    # those totals moved onto the batch strips, which count the rows they head
    # and are already in hand. "N never arrived" stays a header fact: it is
    # arithmetic on the upload's own claim and needs no rows at all.
    session_ids = [d.current_session_id for d in rows if d.current_session_id]
    sessions = {s.id: s for s in db.scalars(select(ExtractionSession).where(ExtractionSession.id.in_(session_ids))).all()} if session_ids else {}

    # The upload switcher. Every upload, so the scope can be changed without
    # going back to the list first -- on a 582-PDF roll the batch page is the
    # only place you are ever going to be, and making it a dead end once you
    # are in it is what made this feel like two separate apps.
    every_upload = db.scalars(
        select(UploadBatch).order_by(UploadBatch.created_at.desc())
    ).all()

    scope = "upload" if focus else ("unassigned" if unassigned else "all")
    log.info(f"[MAIN] Returning {len(rows)} documents, total={total}, scope={scope}")
    print(f"[MAIN] Returning {len(rows)} documents, total={total}, scope={scope}")

    return_path = request.url.path
    if request.url.query:
        return_path = f"{return_path}?{request.url.query}"

    return render(
        request,
        "documents.html",
        rows=rows,
        batch_groups=batch_groups,
        sessions=sessions,
        total=total,
        all_total=all_total,
        summary=summary,
        page=page,
        page_size=page_size,
        notice=notice,
        return_to=return_path,
        focus=focus,
        # The rename control belongs to the single-upload scope. In the
        # all-uploads scope there is no one upload whose name that field could
        # possibly mean.
        can_rename=focus is not None,
        all_uploads=every_upload,
        filters=dict(
            status=status,
            filename=filename,
            date_from=date_from,
            session_id=session_id,
            document_id=document_id,
            open_batch=open_key,
            unassigned=unassigned,
        ),
    )


@app.get("/documents/batches/{batch_key}", response_class=HTMLResponse, dependencies=[Depends(admin)])
def batch_detail(
    request: Request,
    batch_key: str,
    notice: str = "",
    db: Session = Depends(get_db),
):
    """A single batch of PDFs, on its own page.

    The /documents list shows every batch as a strip; opening one used to drop
    its 100 rows into that page inline. That page is now exclusively strips, so
    "View" links here instead. The batch is addressed by the same
    "<upload id>-<batch number>" key the strips build -- unique across every
    upload, because batch numbering restarts at one per upload.

    The rows, the counts, and the progress bar are all computed from the one
    slice of documents this key names, using the same helpers as the list page
    so the two cannot drift: strip says "100" and the page lists 100.
    """
    log.info(f"[MAIN] Batch detail requested for batch_key={batch_key}")
    print(f"[MAIN] Batch detail requested for batch_key={batch_key}")

    # The key is "<upload id>-<batch number>". Split on the last hyphen so a
    # UUID -- which contains hyphens -- is not mis-parsed.
    idx = batch_key.rfind("-")
    if idx < 0:
        raise HTTPException(404, "Malformed batch key")
    upload_id, _, batch_num_str = batch_key[:idx], "-", batch_key[idx + 1:]
    try:
        batch_num = int(batch_num_str)
    except ValueError:
        raise HTTPException(404, "Malformed batch number")
    unassigned = upload_id == "other"
    if unassigned:
        focus = None
    else:
        try:
            focus = db.get(UploadBatch, parse_uuid(upload_id))
        except HTTPException:
            focus = None
    if (focus is None and not unassigned) or batch_num < 1:
        raise HTTPException(404, "Unknown upload or batch")

    chunk_size = config.UPLOAD_BATCH_SIZE
    offset = (batch_num - 1) * chunk_size
    document_scope = (
        Document.upload_batch_id.is_(None)
        if unassigned
        else Document.upload_batch_id == focus.id
    )
    rows = db.scalars(
        select(Document)
        .where(document_scope)
        .order_by(Document.batch_index.asc())
        .offset(offset)
        .limit(chunk_size)
        .options(selectinload(Document.upload_batch))
    ).all()

    counts, pct = _progress_of(rows)
    settled = counts["done"] + counts["active"] + counts["skipped"] + counts["problem"]
    subgroup = {
        "key": batch_key,
        "batch_num": batch_num,
        "size": len(rows),
        "settled": settled,
        "counts": counts,
        "pct": pct,
        "documents": rows,
    }
    group = {
        "upload": focus,
        "registered": len(rows) if unassigned else focus.file_count,
        "on_page": len(rows),
        "counts": counts,
        "pct": pct,
        "subgroups": [subgroup],
    }

    extraction = batch_extraction_stats(db, rows)

    return_to = f"/documents/batches/{batch_key}"
    scope_label = "Not tied to a named upload" if unassigned else focus.label
    parent_url = "/documents?unassigned=true" if unassigned else f"/documents?upload={focus.id}"
    return render(
        request,
        "batch_detail.html",
        rows=rows,
        batch_groups=[group],
        sessions={},
        total=len(rows),
        all_total=0,
        summary={
            "completed": counts["done"],
            "processing": counts["active"],
            "waiting": counts["waiting"],
            "attention": counts["problem"],
            "duplicates": counts["skipped"],
        },
        page=1,
        page_size=None,
        notice=notice or None,
        return_to=return_to,
        focus=focus,
        scope_label=scope_label,
        parent_url=parent_url,
        extraction=extraction,
        can_rename=False,
        all_uploads=[],
        filters=dict(
            status="", filename="", date_from="", session_id="",
            document_id="", open_batch=batch_key,
        ),
    )


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

    # Voter records for this document, from ONE extraction attempt. The point of
    # the bilingual work is seeing names in Hindi and English, so the table
    # belongs on the document page rather than only three clicks away on a unit
    # page.
    #
    # Scoped to a single session, not to document_id. Every retry opens a new
    # session and writes a fresh copy of every card, so filtering on document_id
    # alone lists each voter once per attempt ever made -- twice over for a
    # document that has been retried once, with no way to tell the copies apart.
    records, record_session = records_for_document(db, doc.id, limit=100)
    total_records = count_records_for_document(db, doc.id)

    # What page 1 of the PDF prints, and the OCR counts from the attempt shown
    # above, so the two can be compared on the page.
    cover = db.scalar(select(DocumentCover).where(DocumentCover.document_id == doc.id))
    location = location_for_document(db, doc.id)
    cover_check = session_gender_counts(db, record_session.id if record_session else None)

    log.info(f"[MAIN] Returning document {document_id} with {len(sessions)} sessions, {len(events)} events, {len(records)}/{total_records} records from attempt {record_session.attempt_number if record_session else None}")
    print(f"[MAIN] Returning document {document_id} with {len(sessions)} sessions, {len(events)} events, {len(records)}/{total_records} records from attempt {record_session.attempt_number if record_session else None}")

    return render(
        request,
        "document.html",
        doc=doc,
        sessions=sessions,
        events=events,
        records=records,
        total_records=total_records,
        record_session=record_session,
        cover=cover,
        location=location,
        cover_location_query=cover_location_query(cover) if cover else "",
        cover_check=cover_check,
        cover_max_attempts=config.COVER_MAX_ATTEMPTS,
    )


def _fused(units, session) -> dict:
    """live_progress() for a session, with the OCR card counter merged in.

    The merge is scoped to the session's *running* unit. A session page left
    open while other sessions come and go would otherwise show whatever the
    OCR process happens to be reading, and `fuse_progress` rejects that on the
    page-range test -- but only if it is handed a range to test against, so
    the range always comes from the unit that is actually in flight.
    """
    progress = live_progress(units, pages_total=getattr(session, "pages_total", None))
    running = [
        u for u in units if str(getattr(u, "status", "")) == "processing"
    ]
    page_from = page_to = None
    if running:
        page_from, page_to = running[0].page_from, running[0].page_to
    return fuse_progress(progress, ocr_progress_snapshot(), page_from, page_to)


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
    
    return render(request, "session.html", session=session,
                  doc=db.get(Document, session.document_id), units=units,
                  progress=_fused(units, session))


@app.get("/uploads/{batch_id}", dependencies=[Depends(admin)])
def upload_detail(batch_id: str):
    """Kept so existing links keep working; the page itself moved to /documents.

    This rendered a second template whose list, progress readout, filters and
    selection bar were ~95% identical to /documents -- the same view of a PDF
    list, reachable by two URLs. Rather than keep both and let them drift, the
    upload's own page is now /documents?upload=<id>, and this forwards.

    The redirect is permanent on purpose. These URLs are in bookmarks, in the
    ZIP listing, and in links already pasted into chat; a permanent redirect
    means a browser stops asking after the first visit, and the address stays
    correct if a query string was ever added here later.
    """
    try:
        target = f"/documents?upload={parse_uuid(batch_id)}"
    except HTTPException:
        # A non-uuid here was never a valid upload, and parse_uuid raises.
        raise HTTPException(404, "No such upload")
    return RedirectResponse(target, status_code=301)


@app.get("/documents/{document_id}/pdf", dependencies=[Depends(admin)])
def document_pdf(document_id: str, db: Session = Depends(get_db)):
    """Stream the source PDF inline so it renders in the browser tab.

    source_path is the path the pipeline itself reads from. Serving it with
    inline disposition lets the browser's built-in PDF viewer show the file --
    useful for checking structure before approving extraction on a new batch.
    A missing file (upload dir cleaned, archive not yet restored) is a 404,
    not a stack trace.
    """
    doc = db.get(Document, parse_uuid(document_id))
    if not doc:
        raise HTTPException(404)
    path = Path(doc.source_path)
    if not path.is_file():
        raise HTTPException(404, "PDF file not found on disk")
    return FileResponse(path, media_type="application/pdf", filename=doc.file_name,
                        content_disposition_type="inline")


@app.get("/documents/{document_id}/images", response_class=HTMLResponse, dependencies=[Depends(admin)])
def document_images(request: Request, document_id: str, db: Session = Depends(get_db)):
    doc = db.get(Document, parse_uuid(document_id))
    if not doc:
        raise HTTPException(404)
    assets = assets_for_document(db, doc.id)
    if not assets:
        try:
            assets = extract_visual_assets(db, doc)
        except Exception as exc:
            raise HTTPException(502, f"Could not prepare visual images: {exc}") from exc
    # A cover-derived search link is useful immediately and does not pretend to
    # be an exact coordinate. Exact pins remain separately gated by the local
    # multi-landmark verifier.
    cover = db.scalar(select(DocumentCover).where(DocumentCover.document_id == doc.id))
    return render(
        request,
        "document_images.html",
        doc=doc,
        assets=assets,
        location=location_for_document(db, doc.id),
        cover_location_query=cover_location_query(cover) if cover else "",
        map_verifications=verifications_for_document(db, doc.id),
    )


@app.get("/documents/{document_id}/images/{asset_id}", dependencies=[Depends(admin)])
def document_image_file(document_id: str, asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(DocumentVisualAsset, parse_uuid(asset_id))
    if not asset or str(asset.extraction_id) not in {str(row.extraction_id) for row in assets_for_document(db, parse_uuid(document_id))}:
        raise HTTPException(404)
    body = s3_client().get_object(Bucket=config.MINIO_DERIVED_ASSETS_BUCKET, Key=asset.object_key)["Body"]
    return StreamingResponse(body.iter_chunks(), media_type="image/png")


def _map_anchor_lines(image: bytes) -> list[str]:
    """OCR visible map labels; never treat a result as an exact pin location."""
    with tempfile.NamedTemporaryFile(suffix=".png") as source:
        source.write(image); source.flush()
        result = subprocess.run(
            ["tesseract", source.name, "stdout", "-l", "eng", "--psm", "11"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    lines = [re.sub(r"\s+", " ", value).strip() for value in result.stdout.splitlines()]
    # Short isolated words and obvious map UI labels make bad geocoding input.
    return [line for line in lines if len(line) >= 5 and re.search(r"[A-Za-z]", line)][:12]


@app.post("/documents/{document_id}/images/{asset_id}/locate", dependencies=[Depends(admin)])
def locate_image_on_map(document_id: str, asset_id: str, request: Request, db: Session = Depends(get_db)):
    """Locate a map image on demand, using one cached, rate-limited lookup.

    A static screenshot does not carry its pin's GPS coordinate. This stores
    an *estimated landmark location* only, visibly labelled as such in the UI.
    It intentionally does not run as part of the bulk image extractor.
    """
    # A one-label search proved capable of placing a Loni roll in Mumbai.
    # Keep the endpoint closed until the multi-landmark / exact-source verifier
    # is implemented; a confident-looking wrong pin is worse than no pin.
    raise HTTPException(409, "Automatic map estimation is disabled until the location can be verified from exact source data or multiple landmarks.")
    document = db.get(Document, parse_uuid(document_id))
    asset = db.get(DocumentVisualAsset, parse_uuid(asset_id))
    if not document or not asset or str(asset.extraction_id) not in {str(row.extraction_id) for row in assets_for_document(db, document.id)}:
        raise HTTPException(404)
    if asset.latitude is not None and asset.longitude is not None:
        return RedirectResponse(f"/documents/{document_id}/images", status_code=303)
    image = s3_client().get_object(Bucket=config.MINIO_DERIVED_ASSETS_BUCKET, Key=asset.object_key)["Body"].read()
    anchors = _map_anchor_lines(image)
    # One request per click, one query per second at most per web process. This
    # keeps the public Nominatim endpoint out of our background/bulk workflow.
    location = None
    selected = None
    for index, anchor in enumerate(anchors[:3]):
        if index:
            time.sleep(1.05)
        query = f"{anchor}, India"
        response = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "in"},
            headers={"User-Agent": "electoral-roll-map-location/1.0 (internal-admin)"}, timeout=15,
        )
        response.raise_for_status()
        matches = response.json()
        if matches:
            location, selected = matches[0], anchor
            break
    asset.image_text = "\n".join(anchors) or None
    if location:
        asset.latitude = float(location["lat"])
        asset.longitude = float(location["lon"])
        asset.heading = f"Estimated from map label: {selected}"
    else:
        asset.heading = "No reliable map landmark was recognised"
    db.commit()
    return RedirectResponse(f"/documents/{document_id}/images", status_code=303)


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


@app.get("/folders", response_class=HTMLResponse, dependencies=[Depends(admin)])
def folders(request: Request, db: Session = Depends(get_db)):
    """Manage the folders the pipeline walks looking for PDFs."""
    roots = db.scalars(select(ScanRoot).order_by(ScanRoot.id)).all()
    # The help text teaches whichever mapping this deployment actually uses. On
    # Linux, "C:\..." is not merely unhelpful, it is wrong -- there are no drive
    # letters to rewrite.
    path_placeholder = (
        "/srv/rolls" if config.HOST_MOUNT_STYLE == "posix" else r"C:\Users\pallav\Documents\Rolls"
    )
    return render(request, "folders.html", roots=roots,
                  host_prefix=config.HOST_MOUNT_PREFIX,
                  host_style=config.HOST_MOUNT_STYLE,
                  path_placeholder=path_placeholder)


@app.post("/folders", dependencies=[Depends(admin)])
def folders_add(
    request: Request,
    path: str = Form(...),
    label: str = Form(""),
):
    raw = (path or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "Folder path is required")

    resolved = _resolve_scan_path(raw)

    with SessionLocal.begin() as db:
        existing = db.scalar(select(ScanRoot).where(ScanRoot.path == raw))
        if existing:
            # Re-adding is not an error and does not silently reset the root
            # to disabled -- it is the natural thing to do after fixing a
            # path, so it is made to work rather than refused.
            existing.enabled = True
            existing.last_error = None
            log.info("[MAIN] Re-enabled existing scan root %s", raw)
            return RedirectResponse("/folders", status_code=303)

        db.add(ScanRoot(path=raw, label=(label or "").strip() or None, enabled=True))

    log.info("[MAIN] Added scan root %r -> %s", raw, resolved)
    return RedirectResponse("/folders", status_code=303)


@app.post("/folders/{root_id}/delete", dependencies=[Depends(admin)])
def folders_delete(root_id: int):
    """Remove a scan root. Documents already discovered are NOT deleted --
    they have their own records and their own status, and a folder no longer
    being scanned does not un-discover a file that was legitimately read."""
    with SessionLocal.begin() as db:
        root = db.get(ScanRoot, root_id)
        if not root:
            raise HTTPException(404, "Scan root not found")
        db.delete(root)

    log.info("[MAIN] Deleted scan root %s", root_id)
    return RedirectResponse("/folders", status_code=303)


@app.post("/folders/{root_id}/toggle", dependencies=[Depends(admin)])
def folders_toggle(root_id: int):
    with SessionLocal.begin() as db:
        root = db.get(ScanRoot, root_id)
        if not root:
            raise HTTPException(404, "Scan root not found")
        root.enabled = not root.enabled

    log.info("[MAIN] Toggled scan root %s", root_id)
    return RedirectResponse("/folders", status_code=303)


@app.post("/folders/{root_id}/scan", dependencies=[Depends(admin)])
def folders_scan(root_id: int, db: Session = Depends(get_db)):
    """Walk one root now, then dispatch whatever it turned up."""
    root = db.get(ScanRoot, root_id)
    if not root:
        raise HTTPException(404, "Scan root not found")

    # Release this session before scanning. The scan writes its result back
    # through its own session (_record_scan_result), and that write blocks on
    # this transaction's row lock for ScanRoot -- which is only released when
    # this request ends. Holding it across the scan meant the result was
    # silently discarded every time, so a folder of unreadable cloud
    # placeholders reported "found=0" with no error recorded.
    raw_path = root.path
    db.expunge(root)
    db.commit()

    found = discover(roots=[raw_path])
    dispatch_documents()
    return RedirectResponse(f"/folders?found={found}", status_code=303)


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


@app.get("/documents/{document_id}/retry", dependencies=[Depends(admin)])
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


@app.post("/uploads/document/{document_id}/extract", dependencies=[Depends(admin)])


@app.get("/uploads/document/{document_id}/extract", dependencies=[Depends(admin)])
def upload_extract_one(document_id: str):
    """The row-level 'Extract' item in the ACTION menu.

    Same code path as the bulk button -- one id instead of many -- so a single
    PDF and a ticked selection behave identically.

    GET is registered alongside POST so the menu can use a plain link. The
    upload list is one big <form>, and a <form> inside a <form> is invalid HTML
    that the parser resolves by dropping the inner form and closing the outer
    one early -- which detached every row after the first from #selection-form
    and made "Extract data" submit one arbitrary PDF rather than the selection.
    A link avoids the nesting entirely. That is only tolerable because this
    action is idempotent: a document that is already queued is refused with a
    409 and lands back on /uploads having changed nothing.
    """
    accepted, skipped = select_documents_for_extraction([document_id])
    if not accepted:
        raise HTTPException(409, "That PDF is already running")
    message = f"Started extraction for {document_id}."
    with SessionLocal() as db:
        return _page_for_documents(db, [document_id], message)


@app.post("/uploads/document/{document_id}/delete", dependencies=[Depends(admin)])


@app.get("/uploads/document/{document_id}/delete", dependencies=[Depends(admin)])
def upload_delete_one(document_id: str):
    """GET is registered for the same reason as the extract endpoint above: the
    row menu is a link, because a <form> inside the list's own <form> breaks
    the list. The 409 below is what makes that acceptable -- a document that is
    still running is refused, so a repeat visit to the URL cannot destroy a
    second PDF by accident the way an unguarded GET would.
    """
    deleted, skipped = delete_documents([document_id])
    if not deleted:
        raise HTTPException(409, "That PDF is still running and cannot be deleted")
    with SessionLocal() as db:
        return _page_for_documents(db, [document_id], "PDF deleted.")


@app.get("/uploads/document/{document_id}/pause", dependencies=[Depends(admin)])


@app.post("/uploads/document/{document_id}/pause", dependencies=[Depends(admin)])
def upload_pause_one(document_id: str, db: Session = Depends(get_db)):
    """Pause a pending/retry document so the dispatcher skips it.

    Only pending and retry documents can be paused — queued/processing ones
    are already in flight and pausing them here would have no effect on the
    worker that already holds them. A paused document keeps its approved=True
    flag so resume just flips it back to pending without needing to re-approve.
    """
    doc = db.get(Document, parse_uuid(document_id))
    if not doc:
        raise HTTPException(404, "PDF not found")
    if doc.status not in ("pending", "retry"):
        raise HTTPException(409, "Only pending or retrying PDFs can be paused")
    doc.status = "paused"
    db.commit()
    return _page_for_documents(db, [document_id], "PDF paused.")


@app.get("/uploads/document/{document_id}/resume", dependencies=[Depends(admin)])


@app.post("/uploads/document/{document_id}/resume", dependencies=[Depends(admin)])
def upload_resume_one(document_id: str, db: Session = Depends(get_db)):
    """Resume a paused document by setting it back to pending.

    The dispatcher's next tick will pick it up. approved stays True because
    a paused document was already approved — we are not removing that decision,
    only lifting the pause.
    """
    doc = db.get(Document, parse_uuid(document_id))
    if not doc:
        raise HTTPException(404, "PDF not found")
    if doc.status != "paused":
        raise HTTPException(409, "Only paused PDFs can be resumed")
    doc.status = "pending"
    db.commit()
    return _page_for_documents(db, [document_id], "PDF resumed — it will be picked up shortly.")


@app.post("/uploads/batch/{batch_id}/label", dependencies=[Depends(admin)])
def upload_batch_rename(batch_id: str, label: str = Form(...)):
    """Rename a batch without touching a single file on disk."""
    with SessionLocal.begin() as db:
        row = db.get(UploadBatch, parse_uuid(batch_id))
        if not row:
            raise HTTPException(404, "No such upload")
        row.label = (label or "").strip()[:120] or row.label
    return RedirectResponse("/upload", status_code=303)


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

    # All records for this document, from ONE extraction attempt. See
    # app/record_scope.py for why the session must be pinned: a retried
    # document holds a full copy of its roll per attempt, and an unscoped
    # export writes every copy -- 1062 CSV lines for a 531-voter roll.
    records, record_session = records_for_document(db, doc.id)

    def generate_csv():
        output = io.StringIO()
        # Write UTF-8 BOM so Excel auto-detects encoding (Google Sheets doesn't need it)
        output.write('﻿')
        writer = csv.writer(output)

        # Write header with all fields.
        #
        # attempt_number is prepended so the file says which extraction pass it
        # came from. Without it a reader comparing two exports has no way to
        # tell a re-extraction from a genuine change in the roll.
        attempt = record_session.attempt_number if record_session else ""
        headers = [
            'attempt', 'sno', 'page_number', 'card_index', 'voter_sr_no', 'id_card_no',
            'gender', 'age', 'house_no',
            'voter_first_name', 'voter_middle_name', 'voter_sur_name',
            'relation_name',
            'voter_father_first_name', 'voter_father_middle_name', 'voter_father_last_name',
            'voter_husband_first_name', 'voter_husband_middle_name', 'voter_husband_last_name',
            'voter_mother_first_name', 'voter_mother_middle_name', 'voter_mother_last_name',
            'voter_other_first_name', 'voter_other_middle_name', 'voter_other_last_name',
            'is_deleted',
            'state_code', 'ac_code', 'anubhag_code', 'anubhag_name', 'anubhag_status', 'booth_code', 'pdf_name',
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
                attempt,
                r.sno or '',
                r.page_number or '',
                r.card_index or '',
                r.voter_sr_no or '',
                r.epic_number or '',  # id_card_no = epic_number
                r.gender_hi or '',    # gender = gender_hi
                r.age or '',
                house_number_display(r.house_number_hi),  # source '-' is a declared absence
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
                ('Deleted' if r.is_deleted else 'On the roll') if r.is_deleted is not None else '',
                r.state_code or '',
                r.ac_code or '',
                r.anubhag_code or '',
                r.anubhag_name or '',
                r.anubhag_status or '',
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
                house_number_display(r.house_number_hi),
                house_number_display(r.house_number_en),
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

    # Clean filename and ensure proper encoding.
    #
    # quote() leaves "/" unescaped by default (safe="/"), so a slash in the
    # source filename would truncate the Content-Disposition header at the
    # first slash and hand the browser a name with no extension. The illegal
    # characters are stripped first so the name survives a Windows browser too.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", doc.file_name)
    stem = Path(stem).stem.replace(".pdf", "") or "export"
    safe_filename = f"{stem}_export.csv"
    encoded_filename = quote(safe_filename, safe="")
    
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

    progress = _fused(units, session)

    return {
        "status": session.status,
        "pages_processed": session.pages_processed,
        "pages_total": session.pages_total,
        "records_extracted": session.records_extracted,
        "records_failed": session.records_failed,
        "processing_time_ms": session.processing_time_ms,
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        # session.pages_processed and session.records_extracted are written
        # only by finalize(), so they stay at 0 for the entire run. `progress`
        # is built from the unit rows instead and does move while work is
        # happening. The session-level pair is left in the payload: the
        # document browser and any other consumer still read it, and dropping
        # a field from a response someone already reads is a silent break.
        "progress": progress,
        # Promoted out of `progress` for the "Processing time" readout. Top level
        # because processing_time_ms sits beside it and the two are compared by
        # eye: one is OCR time, the other is wall clock including queue wait.
        "compute_time_ms": progress.get("compute_time_ms"),
        "units": [
            {
                "id": str(u.id),
                "unit_number": u.unit_number,
                "status": u.status,
                "records_extracted": u.records_extracted,
                # Sent so the table's How long column can be recomputed in the
                # browser. It cannot read processing_time_ms: that column is
                # declared on ExtractionSession, not ExtractionUnit, so the
                # value it used to send was always null.
                "started_at": u.started_at.isoformat() if u.started_at else None,
                "completed_at": u.completed_at.isoformat() if u.completed_at else None,
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
    lang: str = "both",
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
        PUBLIC_TABLE_ORDER,
        TABLES,
        Table,
        anubhag_display,
        cell_text,
        columns_present_in,
        default_table,
        get_table,
    )

    chosen = get_table(table) if table in PUBLIC_TABLE_ORDER else default_table()
    PER_PAGE = 100
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

    # Language filter: drop _hi columns for English-only, _en for Hindi-only.
    # Only applied to tables that actually have bilingual columns.
    if lang == "hi":
        chosen = Table(
            name=chosen.name, label=chosen.label, description=chosen.description,
            columns=[c for c in chosen.columns if not c.name.endswith("_en")],
        )
    elif lang == "en":
        chosen = Table(
            name=chosen.name, label=chosen.label, description=chosen.description,
            columns=[c for c in chosen.columns if not c.name.endswith("_hi")],
        )

    # Row counts for every table, so the tab list can show what is in each.
    counts: dict[str, int] = {}
    for name in PUBLIC_TABLE_ORDER:
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
    browse_urls = {
        name: "/browse?" + urlencode({"table": name, "q": q, "lang": lang})
        for name in PUBLIC_TABLE_ORDER
    }
    language_urls = {
        value: "/browse?" + urlencode({"table": chosen.name, "q": q, "lang": value})
        for value in ("both", "hi", "en")
    }
    return render(
        request,
        "browse.html",
        table=chosen,
        tables=[TABLES[n] for n in PUBLIC_TABLE_ORDER],
        counts=counts,
        cells=cells,
        total=total,
        query=q,
        page=page,
        pages=pages,
        per_page=PER_PAGE,
        cell_text=cell_text,
        anubhag_display=anubhag_display,
        lang=lang,
        browse_urls=browse_urls,
        language_urls=language_urls,
        clear_url="/browse?" + urlencode({"table": chosen.name, "lang": lang}),
        export_url="/browse/export-options?" + urlencode({"q": q, "lang": lang}),
    )


@app.get("/browse/rows", dependencies=[Depends(admin)])
def browse_rows_json(
    table: str = "",
    q: str = "",
    page: int = 1,
    lang: str = "both",
    db: Session = Depends(get_db),
):
    """JSON endpoint for the infinite-scroll loader on the browse page.

    Returns one page of rows as a list of objects, plus `has_more` so the
    client knows whether to keep listening for scroll events.
    """
    from fastapi.responses import JSONResponse
    from sqlalchemy import text
    from app.db_browse import (
        TABLE_ORDER,
        PUBLIC_TABLE_ORDER,
        TABLES,
        Table,
        cell_text,
        columns_present_in,
        default_table,
        get_table,
    )

    chosen = get_table(table) if table in PUBLIC_TABLE_ORDER else default_table()
    PER_PAGE = 100
    page = max(1, page)

    actual = {r[0] for r in db.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_name = :t"), {"t": chosen.name})}
    missing = columns_present_in(chosen, actual)
    if missing:
        chosen = Table(
            name=chosen.name, label=chosen.label, description=chosen.description,
            columns=[c for c in chosen.columns if c.name not in missing],
        )

    if lang == "hi":
        chosen = Table(
            name=chosen.name, label=chosen.label, description=chosen.description,
            columns=[c for c in chosen.columns if not c.name.endswith("_en")],
        )
    elif lang == "en":
        chosen = Table(
            name=chosen.name, label=chosen.label, description=chosen.description,
            columns=[c for c in chosen.columns if not c.name.endswith("_hi")],
        )

    col_names = [c.name for c in chosen.columns]
    select_list = ", ".join(col_names)
    params: dict[str, object] = {}

    where = ""
    if q.strip():
        searchable = [c.name for c in chosen.columns
                      if not c.numeric and not c.expandable]
        if searchable:
            clause = " OR ".join(f'"{n}"::text ILIKE :term' for n in searchable)
            where = f" WHERE {clause}"
            params["term"] = f"%{q.strip()}%"

    total_sql = f"SELECT count(*) FROM {chosen.name}{where}"
    total = db.scalar(text(total_sql), params) or 0

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

    rows_out = []
    for row in fetched:
        obj = {}
        for idx, c in enumerate(chosen.columns):
            obj[c.name] = cell_text(row[idx], c.name) if row[idx] is not None else ""
            if c.expandable and row[idx] is not None:
                raw = str(row[idx])
                obj[f"__raw_{c.name}"] = raw[:4000] if len(raw) > 4000 else raw
            if c.name == "is_deleted":
                # The row tint lives on <tr>, so the client needs the bare
                # flag as well as the word.
                obj["__deleted"] = bool(row[idx])
        rows_out.append(obj)

    return JSONResponse({
        "rows": rows_out,
        "page": page,
        "has_more": (page * PER_PAGE) < total,
        "total": total,
    })


@app.get("/browse/export-options", dependencies=[Depends(admin)])
def browse_export_options(
    request: Request,
    q: str = "",
    lang: str = "both",
    db: Session = Depends(get_db),
):
    """Column picker page before a bulk CSV export from the database browser."""
    from sqlalchemy import text
    from app.db_browse import TABLES, export_choices

    er = TABLES["electoral_records"]
    actual = {row[0] for row in db.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'electoral_records'"
    ))}
    columns = [column for column in er.columns if column.name in actual]
    if lang not in {"both", "hi", "en"}:
        lang = "both"
    total = db.scalar(text(
        "SELECT count(*) FROM electoral_records"
        + (" WHERE " + " OR ".join(
            f'"{c.name}"::text ILIKE :term'
            for c in columns if not c.numeric and not c.expandable
        ) if q.strip() else "")
    ), {"term": f"%{q.strip()}%"} if q.strip() else {}) or 0

    return render(request, "browse_export_options.html",
                  columns=columns, choices=export_choices(columns, lang),
                  query=q, total=total, lang=lang,
                  back_url="/browse?" + urlencode({
                      "table": "electoral_records", "q": q, "lang": lang,
                  }))


@app.post("/browse/export")
def browse_export_post(
    request: Request,
    q: str = Form(""),
    lang: str = Form("both"),
    columns: list[str] = Form(default=[]),
    user: AdminUser = Depends(admin),
    db: Session = Depends(get_db),
):
    """Export filtered electoral records as one Excel workbook.

    The page asks for progress with X-Export-Progress. That returns at once
    and the workbook is built in the background. A normal form post still
    waits and downloads the file directly.
    """
    from fastapi.responses import JSONResponse
    from app.browse_export import (
        MEDIA_TYPE,
        ExportBusy,
        ExportRejected,
        choose_columns,
        start_export,
        write_workbook,
    )

    try:
        chosen = choose_columns(db, columns, lang)
    except ExportRejected as exc:
        raise HTTPException(400, str(exc)) from exc

    if request.headers.get("x-export-progress") == "1":
        try:
            job_id = start_export(str(user.id), chosen, q)
        except ExportBusy as exc:
            raise HTTPException(429, str(exc)) from exc
        return JSONResponse({
            "job_id": job_id,
            "status_url": f"/browse/export/jobs/{job_id}",
            "file_url": f"/browse/export/jobs/{job_id}/file",
        })

    temporary_path = write_workbook(db, chosen, q)
    return FileResponse(
        temporary_path,
        media_type=MEDIA_TYPE,
        filename="electoral_records.xlsx",
        background=BackgroundTask(Path(temporary_path).unlink, missing_ok=True),
    )


@app.get("/browse/export/jobs/{job_id}")
def browse_export_job(job_id: str, user: AdminUser = Depends(admin)):
    """How far the current user's workbook has been written."""
    from fastapi.responses import JSONResponse
    from app.browse_export import export_status

    snapshot = export_status(job_id, str(user.id))
    if snapshot is None:
        raise HTTPException(404, "This export is no longer available.")
    return JSONResponse(snapshot)


@app.get("/browse/export/jobs/{job_id}/file")
def browse_export_job_file(job_id: str, user: AdminUser = Depends(admin)):
    """Download a workbook that has finished being written."""
    from app.browse_export import MEDIA_TYPE, export_file

    path = export_file(job_id, str(user.id))
    if path is None:
        raise HTTPException(404, "This export is not ready.")
    return FileResponse(
        path,
        media_type=MEDIA_TYPE,
        filename="electoral_records.xlsx",
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
            'state_code', 'ac_code', 'anubhag_code', 'anubhag_name', 'anubhag_status', 'booth_code', 'pdf_name',
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
                house_number_display(r.house_number_hi),
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
                'Deleted' if r.is_deleted else 'On the roll',
                r.state_code,
                r.ac_code,
                r.anubhag_code,
                r.anubhag_name,
                r.anubhag_status,
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
                house_number_display(r.house_number_hi),
                house_number_display(r.house_number_en),
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
