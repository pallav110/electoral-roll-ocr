"""Build a voter-record workbook without freezing the page that asked for it.

The browser cannot show download progress until the first byte leaves the
server, and a full roll is not ready until every row has been written. The
export therefore runs in its own thread, reports how many rows are done, and
only then hands the finished file to the browser.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db_browse import TABLES, Column, excel_cell

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_DIR = Path(tempfile.gettempdir()) / "electoral-exports"
_KEEP_SECONDS = 30 * 60
_MAX_RUNNING = 2
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportRejected(ValueError):
    """The chosen columns or language cannot produce a workbook."""


class ExportBusy(RuntimeError):
    """Too many workbooks are already being built."""


def choose_columns(db: Session, names: list[str], lang: str) -> list[Column]:
    """Keep only real electoral-record columns, in the browser's display order."""
    if lang not in {"both", "hi", "en"}:
        lang = "both"
    er = TABLES["electoral_records"]
    actual = {row[0] for row in db.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'electoral_records'"
    ))}
    allowed = {column.name for column in er.columns if column.name in actual}
    chosen = [column for column in er.columns if column.name in set(names) & allowed]
    if lang == "hi":
        chosen = [column for column in chosen if not column.name.endswith("_en")]
    elif lang == "en":
        chosen = [column for column in chosen if not column.name.endswith("_hi")]
    if not chosen:
        raise ExportRejected("Choose at least one export column")
    return chosen


def _where(query: str) -> tuple[str, dict]:
    term = query.strip()
    if not term:
        return "", {}
    er = TABLES["electoral_records"]
    searchable = [column.name for column in er.columns if not column.numeric and not column.expandable]
    if not searchable:
        return "", {}
    clause = " WHERE " + " OR ".join(f'"{name}"::text ILIKE :term' for name in searchable)
    return clause, {"term": f"%{term}%"}


def _plain(value):
    """Values openpyxl can store without the slower conversion path."""
    return value is None or type(value) in (str, int, float)


# Excel's standard red. A deleted voter stays in the file, but the whole row
# is red so it cannot be read as someone still on the roll.
_DELETED_COLOR = "FF0000"


def _deleted_flag(raw: list, columns: list[Column], extra: bool) -> tuple[bool, list]:
    """Whether this row is deleted, and the values that belong in the sheet.

    The flag is read from the database boolean before any cell is turned into
    the words "Deleted" / "On the roll". Those words are for the reader; they
    are not how the row is recognised.
    """
    if extra:
        return bool(raw[-1]), raw[:-1]
    index = next(i for i, column in enumerate(columns) if column.name == "is_deleted")
    return bool(raw[index]), raw


def _sheet_row(sheet, values, columns, deleted: bool):
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    font = Font(color=_DELETED_COLOR) if deleted else None
    cells = []
    for value, column in zip(values, columns):
        stored = value if _plain(value) else excel_cell(value, column.name)
        if font is None:
            cells.append(stored)
            continue
        cell = WriteOnlyCell(sheet, value=stored)
        cell.font = font
        cells.append(cell)
    return cells


def write_workbook(db: Session, columns: list[Column], query: str, on_progress=None) -> str:
    """Write one workbook and return its path. `on_progress(phase, written, total)` is optional."""
    from openpyxl import Workbook

    def report(phase: str, written: int, total):
        if on_progress is not None:
            on_progress(phase, written, total)

    where, params = _where(query)
    report("counting", 0, None)
    total = db.scalar(text(f"SELECT count(*) FROM electoral_records{where}"), params) or 0
    report("writing", 0, total)

    names = [column.name for column in columns]
    # The colour decision needs the flag even when that column was left out
    # of the file. It is selected last and never written as its own column.
    extra_flag = "is_deleted" not in names
    selected = [f'"{name}"' for name in names]
    if extra_flag:
        selected.append('"is_deleted"')
    select_list = ", ".join(selected)
    rows_sql = (
        f"SELECT {select_list} FROM electoral_records{where} "
        "ORDER BY page_number NULLS LAST, source_row_number NULLS LAST, id"
    )
    statement = text(rows_sql).execution_options(stream_results=True, yield_per=1000)

    _DIR.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix="electoral-records-browser-", suffix=".xlsx", dir=_DIR, delete=False
    )
    path = temporary.name
    temporary.close()
    workbook = Workbook(write_only=True)
    try:
        sheet = workbook.create_sheet("Voter records")
        sheet.freeze_panes = "A2"
        sheet.append([column.label for column in columns])
        written = 0
        for row in db.execute(statement, params):
            raw = list(row)
            deleted, values = _deleted_flag(raw, columns, extra_flag)
            sheet.append(_sheet_row(sheet, values, columns, deleted))
            written += 1
            if written % 250 == 0:
                report("writing", written, total)
        report("saving", written, total)
        workbook.save(path)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def _sweep(now: float) -> None:
    stale = [job_id for job_id, job in _JOBS.items() if now - job["updated"] > _KEEP_SECONDS and job["status"] != "running"]
    for job_id in stale:
        job = _JOBS.pop(job_id, None)
        if job and job.get("path"):
            Path(job["path"]).unlink(missing_ok=True)


def start_export(owner_id: str, columns: list[Column], query: str) -> str:
    """Start a workbook in the background and return its job id."""
    import secrets

    now = time.time()
    with _LOCK:
        _sweep(now)
        running = sum(1 for job in _JOBS.values() if job["status"] == "running")
        if running >= _MAX_RUNNING:
            raise ExportBusy("Another Excel file is still being prepared. Wait for it to finish, then try again.")
        job_id = secrets.token_urlsafe(18)
        _JOBS[job_id] = {
            "owner": owner_id,
            "status": "running",
            "phase": "counting",
            "written": 0,
            "total": None,
            "error": "",
            "path": "",
            "started": time.monotonic(),
            "updated": now,
        }

    def run() -> None:
        def on_progress(phase: str, written: int, total) -> None:
            with _LOCK:
                job = _JOBS.get(job_id)
                if job is None or job["status"] != "running":
                    return
                job["phase"] = phase
                job["written"] = written
                job["total"] = total
                job["updated"] = time.time()

        try:
            with SessionLocal() as db:
                path = write_workbook(db, columns, query, on_progress)
            with _LOCK:
                job = _JOBS.get(job_id)
                if job is not None and job["status"] == "running":
                    job["path"] = path
                    job["written"] = job["total"] or job["written"]
                    job["status"] = "ready"
                    job["phase"] = "ready"
                    job["updated"] = time.time()
        except Exception:
            log.exception("browse excel export failed")
            with _LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job["status"] = "error"
                    job["phase"] = "error"
                    job["error"] = "The Excel file could not be created. Try again."
                    job["updated"] = time.time()

    threading.Thread(target=run, name=f"browse-export-{job_id[:6]}", daemon=True).start()
    return job_id


def _duration(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"About {seconds} {unit} left."
    minutes = max(1, (seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"About {minutes} {unit} left."


def _comma(value: int) -> str:
    return f"{value:,}"


def export_status(job_id: str, owner_id: str) -> dict | None:
    """Progress the page can show. None when this user has no such job."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["owner"] != owner_id:
            return None
        status = job["status"]
        phase = job["phase"]
        written = job["written"]
        total = job["total"]
        started = job["started"]
        error = job["error"]

    percent = None
    if status == "error":
        message = error or "The Excel file could not be created. Try again."
        percent = 0
    elif phase == "counting" or total is None:
        message = "Counting records…"
    elif phase == "saving":
        percent = 95
        message = "Packaging the Excel file…"
    elif phase == "ready":
        percent = 100
        if not total:
            message = "The file is ready. It contains the column headings only, and the download is starting."
        else:
            message = f"The file is ready ({_comma(total)} records). The download is starting."
    elif not total:
        percent = 90
        message = "No matching records. Packaging the column headings…"
    elif written <= 0:
        percent = 3
        message = f"Reading {_comma(total)} records…"
    else:
        percent = min(92, 5 + int(written * 85 / total))
        remaining = ""
        elapsed = time.monotonic() - started
        if written >= 200 and elapsed >= 1:
            remaining = " " + _duration((total - written) * elapsed / written)
        message = f"Writing records… {_comma(written)} of {_comma(total)}.{remaining}"

    return {
        "status": status,
        "phase": phase,
        "written": written,
        "total": total,
        "percent": percent,
        "message": message,
    }


def export_file(job_id: str, owner_id: str) -> str | None:
    """Path of a finished workbook, or None when it is not ready for this user."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["owner"] != owner_id or job["status"] != "ready" or not job.get("path"):
            return None
        path = job["path"]
    if not Path(path).is_file():
        return None
    return path


MEDIA_TYPE = _XLSX
