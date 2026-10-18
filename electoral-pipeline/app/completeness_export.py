"""Completeness/review workbook helpers."""
from __future__ import annotations

from collections import Counter

_REQUIRED_FIELDS = (
    "epic_number", "name_hi", "relative_name_hi", "house_number_hi",
    "age", "gender_hi",
)


def completeness_rows(records):
    """Return one non-PII summary row per missing/review condition."""
    rows = []
    for record in records:
        missing = [
            field for field in _REQUIRED_FIELDS
            if not str(getattr(record, field, "") or "").strip()
        ]
        reasons = list(getattr(record, "review_reasons", None) or [])
        if missing or reasons or not getattr(record, "is_valid", True):
            rows.append({
                "document_id": str(getattr(record, "document_id", "")),
                "page_number": getattr(record, "page_number", ""),
                "card_index": getattr(record, "card_index", ""),
                "missing_fields": ",".join(missing),
                "review_reasons": ",".join(map(str, reasons)),
                "is_valid": bool(getattr(record, "is_valid", True)),
            })
    return rows


def completeness_summary(records):
    rows = completeness_rows(records)
    counts = Counter()
    for row in rows:
        for field in filter(None, row["missing_fields"].split(",")):
            counts[field] += 1
    return {
        "records": len(records),
        "review_rows": len(rows),
        "missing_fields": dict(counts),
    }
