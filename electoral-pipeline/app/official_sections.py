"""Fail-closed enrichment from an official ECI/CEO section catalogue."""
from __future__ import annotations

import json
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def _value(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> str:
    value = _value(value).upper()
    value = re.sub(r"^S(?=\d+$)", "", value)
    return (value.lstrip("0") or "0") if value else ""


def _revision_from_filename(filename: str) -> str:
    match = re.search(r"(SIR-[^-]+-Revision\d+)", filename, re.IGNORECASE)
    return match.group(1).casefold() if match else ""


@lru_cache(maxsize=2048)
def _file_sha256(path_text: str, mtime_ns: int, size: int) -> str:
    """Hash once per unchanged PDF, not once for every page-range unit."""
    del mtime_ns, size
    digest = hashlib.sha256()
    with open(path_text, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_sha256(path_text: str) -> str:
    path = Path(path_text)
    stat = path.stat()
    return _file_sha256(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_catalog(path_text: str, mtime_ns: int) -> tuple[dict[str, str], ...]:
    """Load only complete rows; a bad row cannot become production metadata."""
    del mtime_ns
    data = json.loads(Path(path_text).read_text(encoding="utf-8"))
    rows = data.get("sections", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("catalogue must be a JSON list or {sections: [...]}")
    required = {"source_filename", "state_code", "ac_code", "booth_code", "roll_revision", "section_number", "section_name_hi", "source_url"}
    clean = []
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            continue
        item = {key: _value(row.get(key)) for key in required | {"source_sha256"}}
        if all(item[key] for key in required):
            clean.append(item)
    return tuple(clean)


def resolve_official_section(*, catalog_path: str, source_filename: str,
                             state_code: Any, ac_code: Any, booth_code: Any,
                             source_sha256: str = "") -> dict[str, str] | None:
    """Return one exact verified match. Any uncertainty returns None."""
    if not catalog_path:
        return None
    path = Path(catalog_path)
    if not path.is_file():
        return None
    filename = Path(source_filename).name
    revision = _revision_from_filename(filename)
    state, ac, booth = _number(state_code), _number(ac_code), _number(booth_code)
    if not all((filename, revision, state, ac, booth)):
        return None
    matches = [row for row in _load_catalog(str(path), path.stat().st_mtime_ns) if (
        Path(row["source_filename"]).name == filename
        and _number(row["state_code"]) == state
        and _number(row["ac_code"]) == ac
        and _number(row["booth_code"]) == booth
        and row["roll_revision"].casefold() == revision
        and (not row["source_sha256"] or row["source_sha256"] == source_sha256)
    )]
    return matches[0] if len(matches) == 1 else None


def enrich_from_official_catalog(roll_metadata: dict[str, Any], records: list[dict[str, Any]], *,
                                 source_filename: str, catalog_path: str,
                                 source_sha256: str = "") -> dict[str, str] | None:
    """Apply a verified official value to metadata and every row in its roll.

    This is deliberately an *override* rather than another OCR vote.  A
    catalogue row is used only after all of filename, revision, state, AC,
    booth, and (when supplied) the PDF checksum match exactly.  That gives a
    future re-upload of a known roll the official section value without ever
    copying a locality from a similarly named, but different, PDF.
    """
    match = resolve_official_section(
        catalog_path=catalog_path, source_filename=source_filename,
        state_code=roll_metadata.get("state_code"), ac_code=roll_metadata.get("ac_code"),
        booth_code=roll_metadata.get("booth_code"), source_sha256=source_sha256)
    if not match:
        return None
    roll_metadata.update({
        "anubhag_code": match["section_number"],
        "anubhag_name": match["section_name_hi"],
        # Keep this in the established per-record field too.  Otherwise the
        # database/UI keeps saying "header needs verification" after a
        # verified catalogue value has replaced the OCR read.
        "anubhag_status": "official_verified",
        "anubhag_review_reason": "",
        "section_metadata_status": "official_verified",
        "section_metadata_source_url": match["source_url"],
    })
    for record in records:
        record["anubhag_code"] = match["section_number"]
        record["anubhag_name"] = match["section_name_hi"]
        record["anubhag_status"] = "official_verified"
        sources = dict(record.get("field_sources") or {})
        sources["anubhag_metadata"] = {
            "kind": "official_section_catalog",
            "source_url": match["source_url"],
        }
        record["field_sources"] = sources
    return match
