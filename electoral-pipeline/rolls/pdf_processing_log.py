"""Structured, human-readable logging for PDF scan / OCR / extraction."""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

# Between INFO (20) and WARNING (30); shown as "SUCCESS" in log output.
SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

_SEPARATOR_MAJOR = "═" * 60
_SEPARATOR_MINOR = "─" * 60
_INDENT = "  "


def success(logger: logging.Logger, msg: str, *args: Any, **kwargs: Any) -> None:
    if logger.isEnabledFor(SUCCESS):
        logger.log(SUCCESS, msg, *args, **kwargs)


def format_timestamp(when: float | None = None) -> str:
    instant = datetime.fromtimestamp(when or time.time(), tz=timezone.utc).astimezone()
    return instant.strftime("%Y-%m-%d %H:%M:%S %z")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}m {remainder:.1f}s"


def format_duration_clock(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}m {secs:02d}s"


def _kv(label: str, value: Any) -> str:
    return f"{_INDENT}{label:<14}: {value}"


def _flush_streams() -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    for handler in logging.root.handlers:
        flush = getattr(handler, "flush", None)
        if callable(flush):
            flush()


def live_row_logging_enabled() -> bool:
    return os.getenv("OCR_LIVE_ROWS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _person_name(
    first: Any,
    middle: Any,
    last: Any,
) -> str:
    return " ".join(
        part.strip()
        for part in (str(first or ""), str(middle or ""), str(last or ""))
        if part and str(part).strip()
    )


def _format_public_record_fields(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Display labels aligned with the public OCR / mapper schema."""
    rows: list[tuple[str, str]] = [
        ("S.No", str(record.get("voter_sr_no") or record.get("sno") or "")),
        ("State Code", str(record.get("state_code") or "")),
        ("AC Code", str(record.get("ac_code") or "")),
        ("Booth Code", str(record.get("booth_code") or "")),
        ("Anubhag Code", str(record.get("anubhag_code") or "")),
        ("Anubhag Name", str(record.get("anubhag_name") or "")),
        ("ID Card No", str(record.get("id_card_no") or "")),
        ("Gender", str(record.get("gender") or "")),
        ("Age", str(record.get("age") or "")),
        ("House No", str(record.get("house_no") or "")),
        (
            "Name",
            _person_name(
                record.get("voter_first_name"),
                record.get("voter_middle_name"),
                record.get("voter_sur_name"),
            ),
        ),
        ("Surname", str(record.get("voter_sur_name") or "")),
        ("Relation", str(record.get("relation_name") or "")),
        (
            "Father's Name",
            _person_name(
                record.get("voter_father_first_name"),
                record.get("voter_father_middle_name"),
                record.get("voter_father_last_name"),
            ),
        ),
        (
            "Husband's Name",
            _person_name(
                record.get("voter_husband_first_name"),
                record.get("voter_husband_middle_name"),
                record.get("voter_husband_last_name"),
            ),
        ),
        (
            "Mother's Name",
            _person_name(
                record.get("voter_mother_first_name"),
                record.get("voter_mother_middle_name"),
                record.get("voter_mother_last_name"),
            ),
        ),
    ]
    if record.get("is_deleted"):
        rows.append(("Deleted", "yes"))
    if record.get("needs_review"):
        reasons = record.get("review_reasons") or []
        rows.append(("Needs Review", ", ".join(str(r) for r in reasons)))
    return rows


def _extraction_failure_reason(record: dict[str, Any] | None, *, slot: bool) -> str:
    if record is None or slot:
        return "no voter fields detected on this card slot"
    reasons = list(record.get("review_reasons") or [])
    if not str(record.get("id_card_no") or "").strip():
        return "ID Card No could not be detected"
    if reasons:
        return "; ".join(str(r) for r in reasons)
    return "record could not be extracted"


class LiveRowProgress:
    """Real-time PDF → page → row logging with field values and DB save tracking."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        pdf_name: str,
        source_path: str = "",
        pdf_index: int | None = None,
        pdf_total: int | None = None,
        session_label: str = "",
    ) -> None:
        self._log = logger
        self.pdf_name = pdf_name
        self.source_path = source_path or pdf_name
        self.pdf_index = pdf_index
        self.pdf_total = pdf_total
        self.session_label = session_label.strip()
        self.started_at = time.time()
        self.total_pages = 0
        self.current_page = 0
        self.cards_per_page = 0
        self.estimated_rows = 0
        self.rows_extracted = 0
        self.rows_saved = 0
        self.rows_failed = 0
        self._page_rows_found = 0
        self._page_rows_extracted = 0
        self._page_rows_saved = 0
        self._page_rows_failed = 0
        self._pending_db: set[int] = set()

    def _emit(self, level: int, msg: str, *args: Any) -> None:
        self._log.log(level, msg, *args)
        _flush_streams()

    def _live_info(self, msg: str, *args: Any) -> None:
        self._emit(logging.INFO, msg, *args)

    def _live_success(self, msg: str, *args: Any) -> None:
        self._emit(SUCCESS, msg, *args)

    def _live_warning(self, msg: str, *args: Any) -> None:
        self._emit(logging.WARNING, msg, *args)

    def _live_error(self, msg: str, *args: Any) -> None:
        self._emit(logging.ERROR, msg, *args)

    def _pdf_label(self) -> str:
        if self.pdf_index is not None and self.pdf_total:
            return f"PDF {self.pdf_index}/{self.pdf_total}"
        return "PDF"

    def _chain_prefix(self) -> str:
        parts: list[str] = []
        if self.session_label:
            parts.append(self.session_label)
        parts.append(self._pdf_label())
        return " → ".join(parts)

    def _progress_line(self, row_number: int) -> None:
        est = self.estimated_rows or "?"
        pct = ""
        if self.estimated_rows and self.estimated_rows > 0:
            pct = f"{100.0 * row_number / self.estimated_rows:.1f}%"
        self._live_info(
            "%s | Page %s/%s | Row %s/%s | Progress: %s | "
            "Total Extracted: %s | Total Saved: %s",
            self._chain_prefix(),
            self.current_page,
            self.total_pages or "?",
            row_number,
            est,
            pct or "—",
            self.rows_extracted,
            self.rows_saved,
        )

    def begin_pdf(self, *, total_pages: int) -> None:
        self.total_pages = total_pages
        self._live_info(_SEPARATOR_MAJOR)
        if self.session_label:
            self._live_info("🗂️  %s", self.session_label)
        if self.pdf_index is not None and self.pdf_total:
            self._live_info(
                "📄 PROCESSING PDF %s/%s",
                self.pdf_index,
                self.pdf_total,
            )
        else:
            self._live_info("📄 PROCESSING PDF")
        self._live_info(_SEPARATOR_MAJOR)
        self._live_info("File: %s", self.pdf_name)
        if self.source_path and self.source_path != self.pdf_name:
            self._live_info("Path: %s", self.source_path)
        self._live_info("Total Pages: %s", total_pages)
        self._live_info(_SEPARATOR_MAJOR)

    def begin_page(self, page_number: int) -> None:
        self.current_page = page_number
        self._page_rows_found = 0
        self._page_rows_extracted = 0
        self._page_rows_saved = 0
        self._page_rows_failed = 0
        self._live_info(
            "%s | [PAGE %s/%s] Processing...",
            self._chain_prefix(),
            page_number,
            self.total_pages or "?",
        )

    def set_page_card_slots(self, card_slots: int) -> None:
        self._page_rows_found = card_slots
        if card_slots and not self.cards_per_page:
            self.cards_per_page = card_slots
            self.estimated_rows = self.total_pages * card_slots

    def on_card_extraction_failed(
        self,
        page_number: int,
        card_index: int,
        reason: str,
    ) -> None:
        self.rows_failed += 1
        self._page_rows_failed += 1
        row_label = f"page {page_number} slot {card_index}"
        self._live_error("[ROW %s] ❌ EXTRACTION FAILED", row_label)
        self._live_error("Reason: %s", reason)
        self._live_warning("Action: Skipping row and continuing extraction")

    def on_row_extracted(self, record: dict[str, Any], page_number: int) -> None:
        row_number = int(record.get("sno") or self.rows_extracted + 1)
        self._progress_line(row_number)
        self._live_info("%s | [ROW %s]", self._chain_prefix(), row_number)
        self._live_info("%s | [ROW %s] Extracting...", self._chain_prefix(), row_number)
        self._live_info("%s | [ROW %s] OCR parsed ✓", self._chain_prefix(), row_number)
        self._live_info(
            "%s | [ROW %s] Fields extracted ✓",
            self._chain_prefix(),
            row_number,
        )
        core_labels = {
            "S.No",
            "State Code",
            "AC Code",
            "Booth Code",
            "Anubhag Code",
            "ID Card No",
            "Gender",
            "Age",
            "House No",
            "Name",
            "Surname",
            "Relation",
            "Father's Name",
        }
        for label, value in _format_public_record_fields(record):
            if label not in core_labels and not value:
                continue
            self._live_info("%s%-14s: %s", _INDENT, label, value or "—")
        if record.get("is_deleted"):
            self.rows_failed += 1
            self._page_rows_failed += 1
            self._live_warning(
                "[ROW %s] Marked deleted — will not be saved to database",
                row_number,
            )
            return
        if not str(record.get("id_card_no") or "").strip():
            reason = _extraction_failure_reason(record, slot=False)
            self.rows_failed += 1
            self._page_rows_failed += 1
            self._live_error("[ROW %s] ❌ EXTRACTION FAILED", row_number)
            self._live_error("Reason: %s", reason)
            self._live_warning("Action: Skipping row and continuing extraction")
            return
        self.rows_extracted += 1
        self._page_rows_extracted += 1
        self._pending_db.add(row_number)
        if record.get("needs_review"):
            self._live_success(
                "[SUCCESS] Row %s extracted (review required, awaiting database save)",
                row_number,
            )
        else:
            self._live_success(
                "[SUCCESS] Row %s extracted (awaiting database save)",
                row_number,
            )

    def on_row_database_saved(self, row_number: int) -> None:
        if row_number in self._pending_db:
            self._pending_db.discard(row_number)
        self.rows_saved += 1
        if self.current_page:
            self._page_rows_saved += 1
        self._live_info(
            "%s | [ROW %s] Database saved ✓",
            self._chain_prefix(),
            row_number,
        )
        self._live_success(
            "[SUCCESS] %s | Row %s extracted and saved",
            self._chain_prefix(),
            row_number,
        )
        self._progress_line(row_number)

    def on_row_database_skipped(self, row_number: int, reason: str) -> None:
        self._pending_db.discard(row_number)
        self.rows_failed += 1
        self._page_rows_failed += 1
        self._live_error("[ROW %s] ❌ DATABASE SAVE FAILED", row_number)
        self._live_error("Reason: %s", reason)
        self._live_warning("Action: Skipping row and continuing extraction")

    def on_row_prepare_failed(self, record: dict[str, Any], reason: str) -> None:
        row_number = int(record.get("sno") or 0)
        self.on_row_database_skipped(row_number, reason)

    def page_summary(self, page_number: int) -> None:
        self._live_info(_SEPARATOR_MINOR)
        self._live_info("PAGE %s SUMMARY", page_number)
        self._live_info("%sRows Found       : %s", _INDENT, self._page_rows_found)
        self._live_info(
            "%sRows Extracted   : %s", _INDENT, self._page_rows_extracted
        )
        self._live_info("%sRows Saved       : %s", _INDENT, self._page_rows_saved)
        self._live_info("%sFailed Rows      : %s", _INDENT, self._page_rows_failed)
        self._live_info(_SEPARATOR_MINOR)

    def pdf_completed(
        self,
        *,
        pages_processed: int,
        rows_extracted: int,
        rows_saved: int,
        rows_failed: int,
        elapsed_seconds: float,
        status: str,
    ) -> None:
        self._live_info(_SEPARATOR_MAJOR)
        self._live_success("✅ PDF COMPLETED")
        self._live_info(_SEPARATOR_MAJOR)
        if self.pdf_index is not None and self.pdf_total:
            self._live_info("%sPDF              : %s/%s", _INDENT, self.pdf_index, self.pdf_total)
        self._live_info("%sFile             : %s", _INDENT, self.pdf_name)
        self._live_info(
            "%sPages Processed  : %s/%s",
            _INDENT,
            pages_processed,
            self.total_pages,
        )
        self._live_info("%sRows Extracted   : %s", _INDENT, f"{rows_extracted:,}")
        self._live_info("%sRows Saved       : %s", _INDENT, f"{rows_saved:,}")
        self._live_info("%sFailed Rows      : %s", _INDENT, f"{rows_failed:,}")
        self._live_info(
            "%sProcessing Time  : %s",
            _INDENT,
            format_duration_clock(elapsed_seconds),
        )
        self._live_info("%sStatus           : %s", _INDENT, status)
        self._live_info(_SEPARATOR_MAJOR)


_live_progress: ContextVar[LiveRowProgress | None] = ContextVar(
    "live_row_progress",
    default=None,
)


def get_live_progress() -> LiveRowProgress | None:
    return _live_progress.get()


@contextmanager
def bind_live_progress(progress: LiveRowProgress | None):
    if progress is None:
        yield
        return
    token: Token = _live_progress.set(progress)
    try:
        yield
    finally:
        _live_progress.reset(token)


class PdfProcessingLog:
    """Lifecycle logger for one PDF (OCR + extraction visibility)."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        pdf_name: str,
        source_path: str = "",
        session_label: str = "",
    ) -> None:
        self._log = logger
        self.pdf_name = pdf_name
        self.source_path = source_path or pdf_name
        self.session_label = session_label.strip()
        self.started_at = time.time()
        self.total_pages = 0
        self.pages_succeeded = 0
        self.pages_failed = 0
        self.pages_skipped = 0
        self.total_records = 0

    def _prefix(self, level: str) -> str:
        if self.session_label:
            return f"[{level}] {self.session_label} → PDF={self.pdf_name!r}"
        return f"[{level}] PDF={self.pdf_name!r}"

    def banner_start(self, *, total_pages: int, phase: str = "scanning") -> None:
        self.total_pages = total_pages
        self._log.info(_SEPARATOR_MAJOR)
        self._log.info("%s  PDF PROCESSING START", self._prefix("INFO"))
        self._log.info(_SEPARATOR_MINOR)
        self._log.info(_kv("File name", self.pdf_name))
        self._log.info(_kv("Full path", self.source_path))
        self._log.info(_kv("Started at", format_timestamp(self.started_at)))
        self._log.info(_kv("Total pages", total_pages))
        self._log.info(_kv("Status", phase))
        self._log.info(_SEPARATOR_MAJOR)

    def phase(self, name: str, detail: str = "") -> None:
        msg = f"{self._prefix('INFO')}  {name}"
        if detail:
            msg = f"{msg} — {detail}"
        self._log.info(msg)

    def page_begin(self, page_number: int) -> None:
        total = self.total_pages or "?"
        self._log.info(
            "%s  Processing Page %s/%s",
            self._prefix("INFO"),
            page_number,
            total,
        )

    def page_skipped(self, page_number: int, reason: str) -> None:
        self.pages_skipped += 1
        self._log.warning(
            "%s  Page %s skipped — %s",
            self._prefix("WARNING"),
            page_number,
            reason,
        )

    def page_records(self, page_number: int, count: int, elapsed_seconds: float) -> None:
        self.pages_succeeded += 1
        self.total_records += count
        self._log.info(
            "%s  Page %s/%s complete — %s record(s) extracted (%s)",
            self._prefix("INFO"),
            page_number,
            self.total_pages or "?",
            count,
            format_duration(elapsed_seconds),
        )

    def page_failed(self, page_number: int, reason: str) -> None:
        self.pages_failed += 1
        self._log.error(
            "%s  Page %s failed — %s",
            self._prefix("ERROR"),
            page_number,
            reason,
        )

    def ocr_started(self) -> None:
        self.phase("OCR started")

    def ocr_completed(self) -> None:
        self.phase("OCR completed")

    def extraction_started(self) -> None:
        self.phase("Data extraction started")

    def extraction_completed(self, record_count: int) -> None:
        self.phase("Data extraction completed", f"{record_count} record(s) mapped")

    def summary(
        self,
        *,
        final_status: str,
        extra_records: int | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        elapsed = elapsed_seconds
        if elapsed is None:
            elapsed = time.time() - self.started_at
        records = extra_records if extra_records is not None else self.total_records
        self._log.info(_SEPARATOR_MAJOR)
        success(
            self._log,
            "%s  PDF PROCESSING SUMMARY",
            self._prefix("SUCCESS"),
        )
        self._log.info(_SEPARATOR_MINOR)
        self._log.info(_kv("File name", self.pdf_name))
        self._log.info(_kv("Full path", self.source_path))
        self._log.info(_kv("Total pages", self.total_pages))
        self._log.info(_kv("Processed OK", self.pages_succeeded))
        self._log.info(_kv("Failed pages", self.pages_failed))
        self._log.info(_kv("Skipped pages", self.pages_skipped))
        self._log.info(_kv("Total records", records))
        self._log.info(_kv("Duration", format_duration(elapsed)))
        self._log.info(_kv("Final status", final_status))
        self._log.info(_SEPARATOR_MAJOR)

    def folder_batch_start(
        self,
        folder: str,
        pdf_count: int,
        action: str,
        *,
        session_label: str = "",
        session_total_pdfs: int | None = None,
    ) -> None:
        self._log.info(_SEPARATOR_MAJOR)
        if session_label:
            self._log.info("[INFO] %s — bulk extraction started", session_label)
        self._log.info("[INFO] FOLDER %s — %s", action.upper(), folder)
        self._log.info(_SEPARATOR_MINOR)
        if session_label:
            self._log.info(_kv("Session", session_label))
        if session_total_pdfs is not None:
            self._log.info(_kv("PDFs in request", session_total_pdfs))
        self._log.info(_kv("PDF count", pdf_count))
        self._log.info(_SEPARATOR_MAJOR)

    def folder_batch_item(
        self,
        index: int,
        total: int,
        pdf_name: str,
        path: str,
        *,
        session_label: str = "",
    ) -> None:
        prefix = f"{session_label} → " if session_label else ""
        self._log.info(
            "[INFO] %sFolder job %s/%s — %s (%s)",
            prefix,
            index,
            total,
            pdf_name,
            path,
        )


def get_pdf_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_page_public_records(
    logger: logging.Logger,
    pdf_name: str,
    page_number: int,
    total_pages: int,
    records: list[dict[str, Any]],
    *,
    skip_if_live: bool = False,
    preview_limit: int | None = None,
) -> None:
    """Log extracted field values for one page (used when live row logging is off)."""
    if skip_if_live:
        return
    if preview_limit is None:
        raw = os.getenv("OCR_PAGE_DATA_PREVIEW_LIMIT", "30").strip()
        try:
            preview_limit = max(0, int(raw))
        except ValueError:
            preview_limit = 30
    logger.info(
        "[PAGE DATA] PDF=%r Page %s/%s — %s record(s) extracted",
        pdf_name,
        page_number,
        total_pages,
        len(records),
    )
    if not records or preview_limit == 0:
        return
    for index, record in enumerate(records[:preview_limit], 1):
        card = record.get("card_index") or index
        logger.info(
            "[PAGE DATA] PDF=%r Page %s slot %s — row %s",
            pdf_name,
            page_number,
            card,
            record.get("sno") or index,
        )
        for label, value in _format_public_record_fields(record):
            if not value and label not in {
                "S.No",
                "ID Card No",
                "Name",
                "Gender",
                "Age",
                "House No",
            }:
                continue
            logger.info("%s%-14s: %s", _INDENT, label, value or "—")
    if preview_limit and len(records) > preview_limit:
        logger.info(
            "[PAGE DATA] … %s more record(s) on page %s (preview limit %s)",
            len(records) - preview_limit,
            page_number,
            preview_limit,
        )
    _flush_streams()
