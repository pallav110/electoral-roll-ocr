"""Do the two display fixes actually fix what they claim?

Both bugs were "the cell is blank", which is the easiest kind of bug to
half-fix: change the template, see a dash instead of an error, and call it
done. These tests pin the value that should appear, so a regression fails
loudly rather than looking like an empty database.

1. duration_between -- ExtractionUnit has no processing_time_ms column, so
   the old templates read a non-existent attribute and got Jinja Undefined,
   which is falsy, which rendered '—' for all 11 units.
2. event_detail -- unit.html and document.html read e.message while
   dashboard.html read e.details. message is NULL for 8 of 10 event types,
   so two of the three tables showed dashes for almost every row.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.presentation import duration_between, event_detail


class TestDurationBetween:
    def test_a_finished_unit_reports_its_real_span(self):
        """The exact case that rendered '—': 12:47:21 to 12:48:29 is 68 sec."""
        started = datetime(2026, 10, 3, 12, 47, 21, tzinfo=timezone.utc)
        completed = datetime(2026, 10, 3, 12, 48, 29, tzinfo=timezone.utc)
        assert duration_between(started, completed) == "1 min 8 sec"

    def test_every_live_unit_in_the_database_gets_a_duration(self):
        """Not one unit in the roll may render a dash."""
        rows = [
            ("12:54:42.664226", "12:54:43.352403"),   # 0.688s
            ("12:47:21.265167", "12:48:29.595313"),   # 68.3s
            ("12:54:43.365302", "12:54:43.860889"),   # 0.496s
            ("12:49:38.327867", "12:50:48.833432"),   # 70.5s
            ("12:52:02.550982", "12:53:14.550915"),   # 72.0s
            ("12:54:17.369197", "12:54:42.648357"),   # 25.3s
        ]
        for start, end in rows:
            s = datetime.fromisoformat(f"2026-10-03T{start}+00:00")
            e = datetime.fromisoformat(f"2026-10-03T{end}+00:00")
            assert duration_between(s, e), f"no duration rendered for {start} -> {end}"

    def test_sub_second_work_does_not_read_as_zero(self):
        """'0 sec' reads as a failure. Speed should not look like breakage."""
        started = datetime(2026, 10, 3, 12, 54, 42, 664226, tzinfo=timezone.utc)
        completed = datetime(2026, 10, 3, 12, 54, 43, 352403, tzinfo=timezone.utc)
        assert duration_between(started, completed) == "under a second"

    def test_a_running_unit_says_nothing_rather_than_zero(self):
        """completed_at is NULL while processing; that is not a 0-second run."""
        started = datetime(2026, 10, 3, 12, 47, 21, tzinfo=timezone.utc)
        assert duration_between(started, None) is None

    def test_missing_start_is_also_nothing(self):
        end = datetime(2026, 10, 3, 12, 48, 29, tzinfo=timezone.utc)
        assert duration_between(None, end) is None

    def test_both_missing_is_nothing(self):
        assert duration_between(None, None) is None

    def test_clock_skew_does_not_produce_a_negative_duration(self):
        """A host whose clock moved backwards must not render '-3 sec'."""
        started = datetime(2026, 10, 3, 12, 50, 0, tzinfo=timezone.utc)
        completed = datetime(2026, 10, 3, 12, 47, 0, tzinfo=timezone.utc)
        assert duration_between(started, completed) is None

    def test_naive_timestamps_are_accepted(self):
        """Postgres returns naive datetimes for these columns on some paths.

        Subtracting a naive from an aware datetime raises TypeError, and a
        template that raises is a blank page for the operator.
        """
        started = datetime(2026, 10, 3, 12, 47, 21)
        completed = datetime(2026, 10, 3, 12, 48, 29)
        assert duration_between(started, completed) == "1 min 8 sec"


class TestEventDetail:
    def test_normalization_completed_shows_its_counts(self):
        """This exact row rendered '—' in unit.html and document.html."""
        details = {"records": 90, "records_expected": 90}
        assert event_detail(details, None) == "records: 90 · records expected: 90"

    def test_unit_started_shows_its_attempt(self):
        assert event_detail({"attempt": 1}, None) == "attempt: 1"

    def test_session_completed_shows_failed_units(self):
        assert event_detail({"failed_units": 0}, None) == "failed units: 0"

    def test_message_is_shown_when_details_is_empty(self):
        assert event_detail(None, "Something happened") == "Something happened"

    def test_both_are_joined_when_both_exist(self):
        out = event_detail({"attempt": 1}, "First try")
        assert out == "attempt: 1 · First try"

    def test_a_genuinely_empty_event_renders_nothing(self):
        """None, so the template's `or '--'` still shows a dash -- correctly."""
        assert event_detail(None, None) is None

    def test_nested_structures_are_skipped(self):
        """A dict-of-dict is not readable in a table cell."""
        out = event_detail({"records": 90, "meta": {"a": 1}, "tags": [1, 2]}, None)
        assert out == "records: 90"

    @pytest.mark.parametrize("details", [None, {}, ""])
    def test_falsy_details_never_crash(self, details):
        """The shapes this column actually holds.

        41 of the 65 live rows store JSON literal null, which SQL reports as
        "not null" but which arrives in Python as None -- the single most
        common input to this function, so it is the one that must be right.
        """
        assert event_detail(details, None) is None

    def test_a_zero_value_is_printed_rather_than_dropped(self):
        """A real 0 is information ('failed_units: 0'), not an empty value."""
        assert event_detail(0, None) == "0"

    def test_a_string_in_message_reads_as_null_is_not_printed(self):
        """'None' as text is noise; absence is already shown by the dash."""
        assert event_detail(None, "None") is None

    def test_every_live_event_type_produces_something(self):
        """Every distinct details payload in the current database."""
        payloads = [
            {"records": 74, "records_expected": 74},
            {"records": 90, "records_expected": 90},
            {"records": 0, "records_expected": 0},
            {"records": 7, "records_expected": 7},
            {"failed_units": 0},
            {"attempt": 1},
        ]
        for payload in payloads:
            assert event_detail(payload, None), f"no detail rendered for {payload}"