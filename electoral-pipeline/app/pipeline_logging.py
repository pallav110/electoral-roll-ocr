"""Structured real-time pipeline log lines (stdout, flushed)."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

from app import config
from app.logging_config import pipeline_log

log = pipeline_log()

_DATA_PREVIEW_LIMIT = max(0, int(os.getenv("PIPELINE_DATA_PREVIEW_LIMIT", "25")))


def _emit(prefix: str, message: str, *args: object) -> None:
    text = (message % args) if args else message
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} | [{prefix}] {text}"
    print(line, flush=True, file=sys.stdout)


def pipeline_event(prefix: str, message: str, *args: object) -> None:
    _emit(prefix.upper(), message, *args)


def extraction_method_label() -> str:
    if config.EXTRACTOR_MODE != "http":
        return config.EXTRACTOR_MODE
    if config.EXTRACTOR_HTTP_STYLE == "ocr_multipart":
        return f"ocr_multipart ({config.EXTRACTOR_URL})"
    return f"http_json ({config.EXTRACTOR_URL})"


def mask_field(name: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    sensitive = {
        "epic_number",
        "epic",
        "id_card_no",
        "voter_sr_no",
        "source_record_hash",
        "identity",
    }
    key = name.lower().replace("-", "_")
    if key in sensitive or "epic" in key or key.endswith("_hash"):
        if len(text) <= 4:
            return "****"
        if len(text) <= 8:
            return f"{text[:2]}…{text[-1:]}"
        return f"{text[:4]}…{text[-2:]}"
    if len(text) > 100:
        return f"{text[:97]}…"
    return text


def record_preview(index: int, item: dict) -> str:
    hi = item.get("hindi") if isinstance(item.get("hindi"), dict) else {}
    en = item.get("english") if isinstance(item.get("english"), dict) else {}
    common = item.get("common") if isinstance(item.get("common"), dict) else {}
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    name = en.get("name") or hi.get("name") or "—"
    relative = en.get("relative_name") or hi.get("relative_name") or "—"
    gender = en.get("gender") or hi.get("gender") or "—"
    house = en.get("house_number") or hi.get("house_number") or "—"
    age = common.get("age")
    epic = mask_field("epic_number", common.get("epic_number"))
    page = source.get("page_number", "?")
    row = source.get("row_number", "?")
    return (
        f"Record {index} (p{page}/r{row}): Name={mask_field('name', name)}, "
        f"EPIC={epic}, Relative={mask_field('name', relative)}, House={mask_field('house', house)}, "
        f"Age={age if age is not None else '—'}, Gender={mask_field('gender', gender)}"
    )


def group_records_by_page(records: list[dict]) -> dict[int, list[dict]]:
    by_page: dict[int, list[dict]] = defaultdict(list)
    for item in records:
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        page = source.get("page_number")
        if isinstance(page, int) and not isinstance(page, bool):
            by_page[page].append(item)
    return dict(by_page)


def log_page_data_preview(page: int, page_total: int, records: list[dict]) -> None:
    pipeline_event("PAGE", "[%s/%s] Extracted %s record(s)", page, page_total, len(records))
    if not records:
        return
    limit = _DATA_PREVIEW_LIMIT
    for idx, item in enumerate(records[:limit], 1):
        pipeline_event("DATA", record_preview(idx, item))
    if limit and len(records) > limit:
        pipeline_event("DATA", "… %s more record(s) on this page (preview limit %s)", len(records) - limit, limit)


def log_pages_for_unit(
    page_from: int,
    page_to: int,
    pages_total: int,
    *,
    stage: str,
    method: str | None = None,
) -> None:
    for page in range(page_from, page_to + 1):
        if stage == "started":
            pipeline_event("PAGE", "[%s/%s] Extraction started", page, pages_total)
            if method:
                pipeline_event("EXTRACT", "[%s/%s] Method: %s", page, pages_total, method)
        elif stage == "completed":
            pipeline_event("PAGE", "[%s/%s] Extraction completed", page, pages_total)


def log_db_batch(
    *,
    page_from: int,
    page_to: int,
    attempted: int,
    inserted: int,
    skipped: int,
    stage: str,
    error: str | None = None,
) -> None:
    page_label = f"{page_from}" if page_from == page_to else f"{page_from}–{page_to}"
    if stage == "started":
        pipeline_event(
            "DB",
            "Extracted %s record(s) — writing to PostgreSQL (pages %s)",
            attempted,
            page_label,
        )
    elif stage == "completed":
        pipeline_event(
            "DB",
            "Successfully saved %s record(s) to PostgreSQL (pages %s)",
            inserted,
            page_label,
        )
        if skipped:
            pipeline_event("DB", "Skipped %s duplicate/conflict record(s) (pages %s)", skipped, page_label)
        pipeline_event("DB", "Insert completed (pages %s)", page_label)
    elif stage == "failed":
        pipeline_event("FAILED", "DB insert failed (pages %s): %s", page_label, error or "unknown error")


def log_progress(
    *,
    pdfs_done: int,
    pdfs_total: int,
    current_pdf: str | None,
    pages_done: int,
    pages_total: int,
    extracted: int,
    saved: int,
) -> None:
    pdf_part = f"PDFs: {pdfs_done}/{pdfs_total}"
    if current_pdf:
        pdf_part += f" ({current_pdf})"
    pipeline_event(
        "PROGRESS",
        "%s | Pages: %s/%s | Extracted: %s | Saved: %s",
        pdf_part,
        pages_done,
        pages_total,
        extracted,
        saved,
    )
