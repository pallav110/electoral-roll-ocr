"""Fill roll-header blanks on a document's records from the rest of the document.

The card reader takes the header (assembly constituency code, section code)
from one page per extraction unit. When that one read fails -- the printed
``1`` comes back as ``]`` -- every row of the unit is saved with the field
blank, although the same value was read cleanly on the neighbouring units of
the same PDF.

These header values are not per-voter facts:

- the AC code is printed identically on every page of a roll, and the cover
  and the ECI filename (``...-S24-53-...``) carry it too;
- the section code only changes at a section boundary, and sections run in
  page order.

So a blank is filled only when the document itself proves the value: one AC
code across all rows that read it (else the cover, else the filename), and a
section code that is the same on the nearest read pages before and after the
blank (or the only one the document has). A page after the last printed
section — an addition list does not reprint the number — keeps the number
already in force. A boundary between two different sections stays blank.
Every filled value is tagged in ``field_sources`` so the provenance stays
visible.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentCover, ElectoralRecord, ExtractionSession

log = logging.getLogger(__name__)

_FILENAME_AC = re.compile(r"-S\d{1,3}-(\d{1,4})-", re.IGNORECASE)
_SECTION_PREFIX = re.compile(r"^\s*(\d{1,4})\s*[-–—]")


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _tag(record: ElectoralRecord, field: str, source: str) -> None:
    sources = dict(record.field_sources or {})
    sources[field] = source
    record.field_sources = sources


def _ac_code(rows: list[ElectoralRecord], cover: DocumentCover | None) -> tuple[str | None, str]:
    read = Counter(r.ac_code.strip() for r in rows if not _blank(r.ac_code))
    if len(read) == 1:
        return next(iter(read)), "filled_from_document"
    if read:
        # Two different AC codes in one roll is a misread somewhere; guessing
        # which one a blank page belongs to would hide it.
        return None, ""
    if cover is not None and not _blank(cover.ac_code):
        return cover.ac_code.strip(), "filled_from_cover"
    match = _FILENAME_AC.search(rows[0].pdf_name or "") if rows else None
    if match:
        return str(int(match.group(1))), "filled_from_filename"
    return None, ""


def _cover_single_section(cover: DocumentCover | None) -> int | None:
    sections = (cover.sections if cover is not None else None) or []
    codes = {int(m.group(1)) for s in sections if isinstance(s, str) and (m := _SECTION_PREFIX.match(s))}
    return codes.pop() if len(codes) == 1 and len(sections) == 1 else None


def _addition_list(record: ElectoralRecord) -> bool:
    """An addition list reprints a title, not a new section number."""
    name = (record.anubhag_name or record.section_name_hi or "").strip()
    return name.startswith("परिवर्धन")


def _section_by_page(rows: list[ElectoralRecord], cover: DocumentCover | None) -> dict[int, int]:
    """Section code for each page whose rows have none, where the document proves it."""
    known: dict[int, set[int]] = {}
    blank_pages: set[int] = set()
    for r in rows:
        if r.page_number is None:
            continue
        # The number on an addition list was carried forward. It is not a
        # second reading of the pages before it, so it must not be the
        # witness that stamps those pages with the previous section.
        if r.anubhag_code is not None and not _addition_list(r):
            known.setdefault(r.page_number, set()).add(r.anubhag_code)
        elif r.anubhag_code is None:
            blank_pages.add(r.page_number)
    if not blank_pages:
        return {}

    all_codes = set().union(*known.values()) if known else set()
    if not all_codes:
        only = _cover_single_section(cover)
        return {p: only for p in blank_pages} if only is not None else {}

    known_pages = sorted(known)
    fill: dict[int, int] = {}
    for page in blank_pages:
        before = [p for p in known_pages if p <= page]
        after = [p for p in known_pages if p >= page]
        # A page that is itself partly read: its last code before / first after.
        prev_code = max(known[before[-1]]) if before else None
        next_code = min(known[after[0]]) if after else None
        if prev_code is not None and next_code is not None:
            if prev_code == next_code:
                fill[page] = prev_code
        elif prev_code is not None:
            # Nothing after this page prints a new section number, so the
            # number already in force continues. An addition list at the end
            # of the part is this case.
            fill[page] = prev_code
        elif len(all_codes) == 1:
            fill[page] = next(iter(all_codes))
    return fill


def fill_header_gaps(db: Session, session_id) -> dict[str, int]:
    """Fill blank AC / section codes on one session's records. Returns counts."""
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    rows = list(db.scalars(
        select(ElectoralRecord).where(ElectoralRecord.session_id == session_id)
    ).all())
    if not rows:
        return {"ac_code": 0, "anubhag_code": 0}
    cover = db.scalar(select(DocumentCover).where(DocumentCover.document_id == rows[0].document_id))

    filled = {"ac_code": 0, "anubhag_code": 0}
    if any(_blank(r.ac_code) for r in rows):
        value, source = _ac_code(rows, cover)
        if value is not None:
            for r in rows:
                if _blank(r.ac_code):
                    r.ac_code = value
                    _tag(r, "ac_code", source)
                    filled["ac_code"] += 1

    for page, code in _section_by_page(rows, cover).items():
        for r in rows:
            if r.page_number == page and r.anubhag_code is None:
                r.anubhag_code = code
                _tag(r, "anubhag_code", "filled_from_neighbouring_pages")
                if r.section_number is None:
                    r.section_number = code
                    _tag(r, "section_number", "filled_from_neighbouring_pages")
                filled["anubhag_code"] += 1

    if filled["ac_code"] or filled["anubhag_code"]:
        log.info("[HEADER_FILL] session %s: filled ac_code=%d anubhag_code=%d",
                 session_id, filled["ac_code"], filled["anubhag_code"])
    return filled


def backfill_all() -> None:
    """One-off: run the fill over every session that already has records."""
    from app.db import SessionLocal

    with SessionLocal.begin() as db:
        session_ids = db.scalars(select(ExtractionSession.id).where(
            ExtractionSession.id.in_(select(ElectoralRecord.session_id).distinct())
        )).all()
        for sid in session_ids:
            result = fill_header_gaps(db, sid)
            if result["ac_code"] or result["anubhag_code"]:
                print(f"session {sid}: ac_code +{result['ac_code']}, anubhag_code +{result['anubhag_code']}")


if __name__ == "__main__":
    backfill_all()
