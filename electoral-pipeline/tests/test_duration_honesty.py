"""Time spent extracting is not the same number as time elapsed.

session.processing_time_ms is completed_at - started_at on the *session*: pure
wall clock, including every second a unit spent queued behind other work. It was
rendered under a label reading "How long it took", so a roll that had done about
5 minutes of OCR reported 7 hr 45 min.

The fix separates the two. compute_time_ms sums each settled unit's own
started_at -> completed_at, which excludes the waiting: the queue wait falls
between units, outside any single unit's span.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.durations import completed_unit_processing_time_ms  # noqa: E402
from app.presentation import live_progress  # noqa: E402

T0 = datetime(2026, 10, 6, 5, 0, 0, tzinfo=timezone.utc)


class _Unit:
    """The attributes live_progress reads. Nothing else is needed."""

    def __init__(self, unit_number, page_from, page_to, status,
                 started_at=None, completed_at=None, records=0):
        self.unit_number = unit_number
        self.page_from = page_from
        self.page_to = page_to
        self.status = status
        self.started_at = started_at
        self.completed_at = completed_at
        self.records_extracted = records
        self.queued_at = None


def _completed(number, minutes, delay_minutes=0):
    start = T0 + timedelta(minutes=delay_minutes)
    return _Unit(number, 1 + (number - 1) * 3, 3 + (number - 1) * 3, "completed",
                 started_at=start, completed_at=start + timedelta(minutes=minutes),
                 records=30)


def test_compute_time_is_the_sum_of_the_units_own_durations():
    """Three units of 10 minutes each is 30 minutes, whatever else was going on."""
    units = [_completed(1, 10), _completed(2, 10), _completed(3, 10)]

    progress = live_progress(units, pages_total=9, at=T0 + timedelta(hours=8))

    assert progress["compute_time_ms"] == 30 * 60 * 1000


def test_queue_wait_is_excluded_from_compute_time():
    """The regression, stated as an assertion.

    Nine units, one minute of real work each, spaced two minutes apart because
    they sat in a queue. The session spans 27 minutes; the extraction was 9.
    Reporting 27 under "How long it took" is what this test exists to prevent.
    """
    units = [_completed(n, 1, delay_minutes=(n - 1) * 2) for n in range(1, 10)]

    progress = live_progress(units, pages_total=27, at=T0 + timedelta(minutes=30))

    assert progress["compute_time_ms"] == 9 * 60 * 1000, (
        "compute_time_ms absorbed queue wait. Each unit took one minute; the "
        "gaps between them are the queue and belong to neither."
    )


def test_persisted_duration_uses_the_same_queue_excluding_calculation():
    units = [_completed(n, 1, delay_minutes=(n - 1) * 60) for n in range(1, 10)]

    assert completed_unit_processing_time_ms(units) == 9 * 60 * 1000


def test_compute_time_is_absent_before_anything_completes():
    """No settled units means no measured rate, so no number is shown.

    Inventing one here would be the same mistake the wall clock made, in the
    other direction.
    """
    units = [_Unit(1, 1, 3, "processing", started_at=T0)]

    assert live_progress(units, pages_total=3, at=T0 + timedelta(minutes=5))[
        "compute_time_ms"
    ] is None


def test_a_failed_unit_does_not_contribute():
    """It says nothing about how fast a good unit runs, or how long the work took."""
    units = [
        _completed(1, 10),
        _Unit(2, 4, 6, "failed", started_at=T0, completed_at=T0 + timedelta(minutes=99)),
    ]

    progress = live_progress(units, pages_total=6, at=T0 + timedelta(hours=2))

    assert progress["compute_time_ms"] == 10 * 60 * 1000


def test_a_still_running_unit_is_not_counted_yet():
    """It has not finished, so its duration is not known. Guessing would inflate."""
    units = [
        _completed(1, 10),
        _Unit(2, 4, 6, "processing", started_at=T0 + timedelta(minutes=20)),
    ]

    progress = live_progress(units, pages_total=6, at=T0 + timedelta(minutes=50))

    assert progress["compute_time_ms"] == 10 * 60 * 1000
