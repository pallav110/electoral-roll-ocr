"""Duration calculations shared by persistence and presentation."""
from __future__ import annotations

from datetime import datetime, timezone


def _aware(value) -> datetime | None:
    """Normalize database datetimes and ISO strings; reject bad values."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def completed_unit_processing_time_ms(units) -> int | None:
    """Return actual OCR time, excluding gaps while units waited in a queue.

    A session's start-to-finish wall clock is not processing time: units can
    spend hours pending between those timestamps.  Each completed unit's own
    start-to-finish span is the interval during which OCR was actually active.
    """
    total_seconds = 0.0
    measured = False
    for unit in units or ():
        if str(getattr(unit, "status", "")) != "completed":
            continue
        started = _aware(getattr(unit, "started_at", None))
        completed = _aware(getattr(unit, "completed_at", None))
        if not started or not completed:
            continue
        seconds = (completed - started).total_seconds()
        if seconds < 0:
            continue
        total_seconds += seconds
        measured = True
    return int(total_seconds * 1000) if measured else None
