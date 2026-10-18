"""Dashboard metric contracts: wall time, unit time, pages, and ETA differ."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.main import _dashboard_timing_metrics

UTC = timezone.utc


def unit(status, page_from, page_to, started, completed, records=0):
    return SimpleNamespace(
        status=status,
        page_from=page_from,
        page_to=page_to,
        started_at=started,
        completed_at=completed,
        records_extracted=records,
    )


def document(status, started, completed):
    return SimpleNamespace(status=status, processing_started_at=started,
                           processing_completed_at=completed)


def test_document_duration_is_wall_clock_not_sum_of_units():
    start = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    done = datetime(2026, 10, 19, 1, 10, tzinfo=UTC)
    # Units overlap, so their sum is 12 minutes while the document took 10.
    units = [
        unit("completed", 3, 5, start, start + timedelta(minutes=6), 90),
        unit("completed", 6, 8, start + timedelta(minutes=2), done, 90),
    ]
    metrics = _dashboard_timing_metrics(
        [document("completed", start, done)], units, total_pages=6,
        completed_pages=6, completed_documents=1, remaining_pages=0,
    )
    assert metrics["avg_document_wall_seconds"] == 600
    assert metrics["unit_seconds_per_page"] == 140


def test_eta_uses_remaining_pages_not_remaining_documents():
    start = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    done = datetime(2026, 10, 19, 1, 10, tzinfo=UTC)
    units = [unit("completed", 3, 8, start, done, 180)]
    metrics = _dashboard_timing_metrics(
        [document("completed", start, done)], units, total_pages=20,
        completed_pages=6, completed_documents=1, remaining_pages=14,
    )
    assert metrics["unit_seconds_per_page"] == 100
    assert metrics["eta_seconds"] == 1400
    assert metrics["pages_remaining"] == 14


def test_zero_completed_units_has_unknown_timing():
    metrics = _dashboard_timing_metrics([], [], total_pages=20,
                                        completed_pages=0,
                                        completed_documents=0,
                                        remaining_pages=20)
    assert metrics["avg_document_wall_seconds"] is None
    assert metrics["unit_seconds_per_page"] is None
    assert metrics["eta_seconds"] is None