from datetime import datetime, timezone, timedelta

from app.db_browse import cell_text, excel_cell


def test_cell_text_renders_datetimes_in_ist():
    assert cell_text(datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc)) == "30 Sep 2026, 19:35:00 IST"


def test_cell_text_treats_naive_as_utc():
    assert cell_text(datetime(2026, 9, 30, 14, 5)) == "30 Sep 2026, 19:35:00 IST"


def test_cell_text_crosses_date_boundary():
    assert cell_text(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)) == "01 Oct 2026, 04:00:00 IST"


def test_excel_cell_writes_naive_ist():
    value = excel_cell(datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc))
    assert value == datetime(2026, 9, 30, 19, 35)
    assert value.tzinfo is None
